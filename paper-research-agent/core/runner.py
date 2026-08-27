"""파이프라인 실행기. CLI(main.py)와 웹 서버(server.py)가 공유한다.

오케스트레이션 로직을 한 곳에 두기 위해 분리했다. UI가 둘로 늘어난다고
[0]~[9] 흐름이 둘이 되면, 한쪽만 고쳐지는 사고가 반드시 난다.

진행 상황은 `on_event` 콜백으로 내보낸다. CLI는 콘솔에 찍고, 서버는
큐에 쌓아 브라우저로 폴링해 간다.
"""

from __future__ import annotations

import copy
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.cache import LookupCache, NullCache
from core.check_oa import OAChecker
from core.core_client import CoreClient
from core.check_venue_tier import venue_tier_detail
from core.export_bib import export_all
from core.fetch_pdf import NotAPdfError, PdfDownloadError, fetch_pdf
from core.http_client import Aborted
from core.indexes import ArxivClient, CrossrefClient, OpenAlexClient
from core.llm_client import LLMError
from core.models import (
    FullTextFailure,
    NoteRecord,
    OAResult,
    SessionLogEntry,
    Summary,
    VerificationResult,
)
from core.graph import (
    build_citation_edges,
    build_keyword_index,
    keywords_for,
    write_keyword_notes,
)
from core.obsidian_writer import (
    enrich_note,
    make_session_name,
    write_note,
    write_session_log,
    write_session_moc,
)
from core.parse_pdf import PdfParseError, ScannedPdfError, build_paged_text, parse_pdf
from core.search_apis import dedupe, search_all
from core.seeds import expand_from_seeds, extract_vocabulary, resolve_seeds
from core.summarize import (
    EmptyTextError,
    check_relevance,
    expand_queries,
    summarize,
    summarize_abstract,
)
from core.verify_paper import Verifier
from core.verify_quotes import verify_quotes

log = logging.getLogger(__name__)

EventFn = Callable[[str, dict], None]
"""on_event(kind, payload). kind: log | query | paper | stage | done"""


class Cancelled(Exception):
    """사용자가 중단을 요청했다."""


class _SkipSummary(Exception):
    """LLM 장애로 요약을 건너뛴다 (내부 신호)."""


@dataclass
class RunResult:
    question: str
    records: list[NoteRecord] = field(default_factory=list)
    uncertain: list[VerificationResult] = field(default_factory=list)
    not_found: list[VerificationResult] = field(default_factory=list)
    irrelevant: list[VerificationResult] = field(default_factory=list)
    seed_papers: list = field(default_factory=list)
    seed_missing: list[str] = field(default_factory=list)
    seed_vocabulary: list[str] = field(default_factory=list)
    log_entries: list[SessionLogEntry] = field(default_factory=list)
    moc_path: str = ""
    log_path: str = ""
    export_paths: list[str] = field(default_factory=list)
    session_dir: str = ""

    @property
    def summarized_count(self) -> int:
        """**전문 기반만** 센다. 초록 기반을 여기 넣으면 신뢰 수준이 뭉개진다."""
        return sum(1 for r in self.records if r.summary and r.summary.depth == "full_text")

    @property
    def abstract_only_count(self) -> int:
        return sum(1 for r in self.records if r.summary and r.summary.depth == "abstract_only")


def _noop(kind: str, payload: dict) -> None:
    pass


_FAILURE_LABEL = {
    "skipped": "전문 확보 끔",
    "no_oa_link": "무료 사본 없음",
    "download_failed": "다운로드 실패",
    "not_a_pdf": "PDF가 아님(랜딩/페이월)",
    "scanned_or_empty": "스캔본",
    "parse_failed": "PDF 해석 실패",
    "summarize_failed": "요약 실패",
}


def _failure_tally(result: "RunResult") -> dict[str, int]:
    """전문 확보 실패를 원인별로 센다.

    이 표가 다음에 뭘 고칠지 알려준다:
      무료 사본 없음이 대부분  → 소스를 늘려야 한다
      PDF가 아님이 대부분      → 랜딩 페이지 처리를 손봐야 한다
      다운로드 실패가 대부분   → 봇 차단 회피가 필요하다
    """
    tally: dict[str, int] = {}
    for r in result.records:
        if r.fulltext_failure != "none":
            key = _FAILURE_LABEL.get(r.fulltext_failure, r.fulltext_failure)
            tally[key] = tally.get(key, 0) + 1
    return tally


def _verify_many(
    candidates: list,
    verifier: Verifier,
    workers: int = 4,
    cancel: threading.Event | None = None,
) -> list[VerificationResult]:
    """후보 여러 건을 병렬로 검증한다.

    검증 시간의 대부분은 계산이 아니라 **대기**다 — HTTP 응답을 기다리고,
    인덱스에 대한 예의상 최소 간격을 지키느라 잔다. 그래서 스레드로 충분하다.

    레이트 리밋은 `ThrottledClient`가 락으로 지키므로, 스레드를 늘려도
    인덱스에 보내는 초당 요청 수는 그대로다. 늘어나는 건 '기다리는 일을
    동시에 기다리는' 정도다.

    입력 순서를 유지해서 반환한다. 결과 순서가 실행할 때마다 달라지면
    로그를 비교하기 어렵다.
    """
    if not candidates:
        return []
    if len(candidates) == 1 or workers <= 1:
        # 순차 경로에서도 Aborted가 새어 나가면 안 된다. 위쪽 except가
        # 잡긴 하지만, 여기서 멈추면 이미 처리한 결과까지 버려진다.
        out: list[VerificationResult] = []
        for c in candidates:
            try:
                out.append(verifier.verify(c))
            except Aborted:
                break
            except Exception as e:  # noqa: BLE001
                log.warning("검증 중 예외 (%s): %s", c.title[:50], e)
                out.append(VerificationResult(
                    candidate=c, status="uncertain",
                    reason=f"검증 중 오류가 발생해 판정하지 못했습니다: {type(e).__name__}",
                ))
        return out

    results: list[VerificationResult | None] = [None] * len(candidates)

    pool = ThreadPoolExecutor(max_workers=min(workers, len(candidates)))
    try:
        futures = {pool.submit(verifier.verify, c): i for i, c in enumerate(candidates)}
        for fut in as_completed(futures):
            if cancel is not None and cancel.is_set():
                # 아직 시작 안 한 작업은 취소한다. `with` 블록으로 두면
                # 종료할 때 실행 중인 것까지 전부 기다리느라 중단이 수십 초씩
                # 걸린다. 이미 돌고 있는 것은 abort 신호를 받아 스스로 끝난다.
                for f in futures:
                    f.cancel()
                break
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Aborted:
                break
            except Exception as e:  # noqa: BLE001 — 한 건 실패가 전체를 죽이면 안 된다
                log.warning("검증 중 예외 (%s): %s", candidates[i].title[:50], e)
                results[i] = VerificationResult(
                    candidate=candidates[i],
                    status="uncertain",
                    reason=f"검증 중 오류가 발생해 판정하지 못했습니다: {type(e).__name__}",
                )
    finally:
        # wait=False: 실행 중인 작업의 완료를 기다리지 않고 즉시 반환한다.
        pool.shutdown(wait=False, cancel_futures=True)

    return [r for r in results if r is not None]


def run_pipeline(
    question: str,
    cfg: dict,
    on_event: EventFn | None = None,
    cancel: threading.Event | None = None,
    seeds: list[str] | None = None,
) -> RunResult:
    """[0]~[9] 전체 실행.

    seeds: 사용자가 "확실히 관련 있다"고 지목한 논문 (DOI 또는 제목).
    주어지면 [0-1]에서 그 논문들의 학술 어휘와 인용 그래프를 먼저 확보한 뒤
    검색에 들어간다.
    """
    emit = on_event or _noop
    result = RunResult(question=question)

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    model = cfg.get("llm", {}).get("model", "(미설정)")
    max_rounds = int(cfg.get("search", {}).get("max_rounds", 3))
    target = int(cfg.get("search", {}).get("target_verified", 5))

    # 공유 인프라
    cache_cfg = cfg.get("cache", {})
    cache = (
        LookupCache(
            cache_cfg.get("path", "~/.cache/paper-agent/lookup.db"),
            ttl_days=int(cache_cfg.get("ttl_days", 90)),
        )
        if cache_cfg.get("enabled", True)
        else NullCache()
    )
    mailto = cfg.get("contact_email")
    # 중단 신호를 모든 네트워크 클라이언트에 넘긴다. 이게 없으면 arXiv의
    # 3초 대기나 재시도 백오프(최대 30초) 도중에는 중단이 안 먹는다.
    openalex = OpenAlexClient(cache=cache, mailto=mailto, abort=cancel)
    crossref = CrossrefClient(cache=cache, mailto=mailto, abort=cancel)
    arxiv = ArxivClient(cache=cache, abort=cancel)
    verifier = Verifier(cfg, openalex=openalex, crossref=crossref, arxiv=arxiv)
    oa_checker = OAChecker(cfg, abort=cancel)
    core_client = CoreClient(cfg, abort=cancel)
    # LLM 장애를 세션 전체에서 기억한다 (논문마다 재시도 방지)
    llm_state: dict = {"summarize_down": False}

    # 이번 실행의 결과를 담을 폴더를 정한다. 검색마다 폴더가 따로 생겨야
    # 어느 논문이 어느 질문에서 나왔는지 알 수 있다.
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("obsidian", {})["_session_dir"] = make_session_name(question)
    result.session_dir = cfg["obsidian"]["_session_dir"]
    emit("log", {"text": f"결과 폴더: {result.session_dir}"})

    pdf_dir = Path(cfg.get("paths", {}).get("pdf_dir", "./downloads")).expanduser()
    pdf_dir.mkdir(parents=True, exist_ok=True)

    used_queries: list[str] = []
    seen_keys: set[str] = set()
    vocabulary: list[str] = []
    seed_candidates: list = []

    # --- [0-1] 시드 논문 ------------------------------------------------------
    # try 안에 둔다. 시드는 부가 기능이라 여기서 터져도 본 탐색은 계속돼야 한다.
    if seeds:
        try:
            emit("stage", {"stage": "seeds"})
            emit("log", {"text": f"시드 논문 {len(seeds)}건 확인 중"})
            found, missing = resolve_seeds(seeds, openalex)
            result.seed_papers = found
            result.seed_missing = missing

            for m in missing:
                emit("log", {"text": f"시드를 찾지 못함: {m[:70]}", "level": "warn"})

            if found:
                for c in found:
                    emit("log", {"text": f"시드 확인: {c.title[:70]} ({c.year or 'n.d.'})"})

                vocabulary = extract_vocabulary(found)
                result.seed_vocabulary = vocabulary
                if vocabulary:
                    emit("log", {"text": f"분야 용어 추출: {', '.join(vocabulary[:8])}"})

                # 인용 그래프 — 키워드 검색이 못 찾는 논문이 여기서 나온다
                sc = cfg.get("seeds", {})
                if sc.get("follow_citations", True):
                    check_cancel()
                    emit("stage", {"stage": "citations"})
                    seed_candidates = expand_from_seeds(
                        found, openalex,
                        use_references=bool(sc.get("use_references", True)),
                        use_citing=bool(sc.get("use_citing", True)),
                    )
                    seed_candidates = dedupe(seed_candidates)
                    # 시드 자신은 후보에서 뺀다
                    seed_keys = {c.identity_key() for c in found}
                    seed_candidates = [c for c in seed_candidates
                                       if c.identity_key() not in seed_keys]
                    emit("log", {
                        "text": f"인용 그래프에서 후보 {len(seed_candidates)}건 확보 "
                                f"(참고문헌 + 피인용)"
                    })
            else:
                emit("log", {"text": "확인된 시드가 없어 질문만으로 검색합니다", "level": "warn"})
        except Cancelled:
            emit("log", {"text": "시드 처리 중 중단됨", "level": "warn"})
            seed_candidates = []
        except Exception as e:  # noqa: BLE001 — 시드 실패가 본 탐색을 막으면 안 된다
            log.warning("시드 처리 실패: %s", e, exc_info=True)
            emit("log", {
                "text": f"시드 처리 중 오류 — 질문만으로 검색합니다 ({type(e).__name__})",
                "level": "warn",
            })
            seed_candidates = []
            vocabulary = []

    try:
        for rnd in range(1, max_rounds + 1):
            check_cancel()
            if len(result.records) >= target:
                break

            emit("stage", {"stage": "expand", "round": rnd})
            emit("log", {"text": f"[{rnd}회차] 검색 쿼리 생성 중"})

            try:
                queries = expand_queries(question, cfg, exclude=used_queries,
                                         vocabulary=vocabulary)
            except LLMError as e:
                emit("log", {"text": f"쿼리 생성 실패: {e}", "level": "error"})
                if rnd == 1:
                    emit("log", {"text": "원 질문을 쿼리로 사용합니다", "level": "warn"})
                    queries = [question]
                else:
                    break

            used_queries.extend(queries)
            emit("query", {"queries": queries, "round": rnd})

            for q in queries:
                check_cancel()
                if len(result.records) >= target:
                    break

                emit("stage", {"stage": "search", "query": q})
                candidates = search_all(q, cfg, openalex=openalex, arxiv=arxiv)

                # 인용 그래프 후보를 첫 쿼리에 한 번만 합류시킨다.
                # 매 쿼리마다 넣으면 같은 후보를 반복 처리하게 된다.
                if seed_candidates:
                    candidates = dedupe(seed_candidates + candidates)
                    emit("log", {"text": f"인용 그래프 후보 {len(seed_candidates)}건 합류"})
                    seed_candidates = []
                entry = SessionLogEntry(
                    query=q, candidates_found=len(candidates), model_used=str(model)
                )
                emit("log", {"text": f"'{q}' → 후보 {len(candidates)}건"})

                emit("stage", {"stage": "verify", "query": q})

                # 검증은 후보끼리 독립적이라 병렬로 돌린다. 대기 시간이
                # 대부분이라(HTTP 응답 + 예의상 간격) 스레드로 충분하다.
                # 스로틀은 클라이언트 안에서 락으로 지켜지므로 한도를 넘지 않는다.
                fresh = []
                for cand in candidates:
                    key = cand.identity_key()
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    fresh.append(cand)

                verified_results = _verify_many(
                    fresh, verifier, workers=int(cfg.get("search", {}).get("verify_workers", 4)),
                    cancel=cancel,
                )

                # LLM이 죽어 있으면 관련성 판정을 건너뛴다. 논문마다 3회씩
                # 재시도하면 후보 40건에 2분 넘게 재시도에만 쓴다.
                relevance_down = False

                for vr in verified_results:
                    check_cancel()
                    if len(result.records) >= target:
                        break

                    if vr.status == "verified":
                        if relevance_down:
                            is_relevant, reason = True, ""
                        else:
                            try:
                                is_relevant, reason = check_relevance(question, vr.candidate, cfg)
                            except LLMError as e:
                                relevance_down = True   # 이 세션에서는 더 시도하지 않는다
                                is_relevant, reason = True, ""
                                emit("log", {
                                    "text": f"관련성 판단 불가 — 이후 논문은 모두 통과시킵니다 ({e})",
                                    "level": "warn",
                                })

                        if not is_relevant:
                            vr.status = "irrelevant"
                            vr.reason = reason or "질문과 관련 없다고 판단됨"
                            result.irrelevant.append(vr)
                            emit("paper", _reject_payload(vr))
                            continue
                    
                        entry.verified_count += 1
                        rec = _process_verified(vr, cfg, oa_checker, pdf_dir, emit,
                                                check_cancel, core_client, llm_state)
                        result.records.append(rec)
                        emit("paper", _paper_payload(rec, vr))
                    elif vr.status == "uncertain":
                        entry.uncertain_count += 1
                        result.uncertain.append(vr)
                        emit("paper", _reject_payload(vr))
                    else:
                        entry.not_found_count += 1
                        result.not_found.append(vr)
                        emit("paper", _reject_payload(vr))

                result.log_entries.append(entry)

    except (Cancelled, Aborted):
        emit("log", {"text": "중단했습니다. 여기까지의 결과를 저장합니다", "level": "warn"})

    # --- [8-1] 그래프 구조 --------------------------------------------------
    # 논문 간 인용 관계는 전부 모아봐야 알 수 있어서 여기서 처리한다.
    # 그래프는 부가 기능이라, 실패해도 노트는 이미 저장돼 있다.
    if result.records and cfg.get("graph", {}).get("enabled", True):
        try:
            emit("stage", {"stage": "graph"})
            kw_index = build_keyword_index(result.records)

            edges: dict = {}
            aborted = cancel is not None and cancel.is_set()
            if cfg.get("graph", {}).get("citation_edges", True) and not aborted:
                edges = build_citation_edges(result.records, openalex)
            elif aborted:
                # 인용 관계 조회는 논문 수만큼 네트워크 왕복이 든다.
                # 중단을 눌렀는데 이게 계속 돌면 "중단이 안 먹는다"가 된다.
                emit("log", {"text": "중단 요청 — 인용 관계 조회는 건너뜁니다", "level": "warn"})

            enriched = 0
            for r in result.records:
                stem = Path(r.note_path).stem
                e = edges.get(stem, {})
                if enrich_note(r.note_path, keywords_for(r, kw_index),
                               e.get("cites", []), e.get("cited_by", [])):
                    enriched += 1

            kw_paths = write_keyword_notes(kw_index, cfg)
            n_edges = sum(len(v.get("cites", [])) for v in edges.values())
            emit("log", {
                "text": f"그래프 구조 생성 — 키워드 허브 {len(kw_paths)}개, "
                        f"논문 간 인용 {n_edges}건, 노트 {enriched}개 갱신"
            })
        except Exception as e:  # noqa: BLE001 — 그래프 실패가 결과를 못 쓰게 하면 안 된다
            log.warning("그래프 생성 실패: %s", e, exc_info=True)
            emit("log", {
                "text": f"그래프 생성 실패 (노트는 정상 저장됨): {e}", "level": "warn",
            })

    # [9] 세션 로그 — 중단됐어도 저장한다.
    #
    # **여기가 터지면 작업 스레드가 죽고 job이 영원히 running으로 남는다.**
    # 그러면 UI는 완료 신호를 못 받아 멈춘 것처럼 보이고, 사용자는
    # "서버가 꺼졌다"고 느낀다. 저장은 무슨 일이 있어도 끝까지 간다.
    try:
        emit("stage", {"stage": "save"})
        result.moc_path = write_session_moc(
            question, result.records, result.uncertain, result.not_found, cfg,
            irrelevant=result.irrelevant, failure_tally=_failure_tally(result),
            seed_papers=result.seed_papers, seed_vocabulary=result.seed_vocabulary,
        )
        result.log_path = write_session_log(result.log_entries, cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("세션 노트 저장 실패: %s", e, exc_info=True)
        emit("log", {"text": f"세션 노트 저장 실패: {e}", "level": "error"})

    # [9-1] Zotero 내보내기 — 서지 관리 프로그램으로 넘길 형식
    try:
        result.export_paths = export_all(question, result.records, cfg,
                                         uncertain=result.uncertain)
        if result.export_paths:
            names = ", ".join(Path(x).suffix.lstrip(".") for x in result.export_paths)
            emit("log", {"text": f"Zotero 내보내기 완료 ({names}) — exports 폴더"})
    except Exception as e:  # noqa: BLE001 — 내보내기 실패가 결과를 못 쓰게 하면 안 된다
        log.warning("내보내기 실패: %s", e, exc_info=True)
        emit("log", {"text": f"내보내기 실패 (노트는 정상 저장됨): {e}", "level": "warn"})
    try:
        cache.close()
    except Exception:  # noqa: BLE001
        pass

    emit(
        "done",
        {
            "verified": len(result.records),
            "summarized": result.summarized_count,
            "abstract_only": result.abstract_only_count,
            "uncertain": len(result.uncertain),
            "not_found": len(result.not_found),
            "irrelevant": len(result.irrelevant),
            "fulltext_failures": _failure_tally(result),
            "moc_path": result.moc_path,
            "log_path": result.log_path,
            "exports": result.export_paths,
            "session_dir": result.session_dir,
        },
    )
    return result


# ---------------------------------------------------------------------------


def _process_verified(
    vr: VerificationResult,
    cfg: dict,
    oa_checker: OAChecker,
    pdf_dir: Path,
    emit: EventFn,
    check_cancel: Callable[[], None],
    core_client: CoreClient | None = None,
    llm_state: dict | None = None,
) -> NoteRecord:
    """검증 통과 후보 하나를 [4]~[8]까지.

    전문 확보 순서:
      1. CORE — 링크가 아니라 **텍스트를 직접** 준다. 다운로드·파싱 단계를
         통째로 건너뛰므로 실패할 수 있는 지점이 두 개 사라진다.
      2. OA 링크(OpenAlex / arXiv / Unpaywall / Semantic Scholar) → PDF 다운로드

    실패하면 그 **이유를 분류해 기록**한다. "전문이 안 구해진다"는 하나의
    증상만 보면 소스를 늘려야 할지 다운로드를 고쳐야 할지 알 수 없다.
    """
    cand = vr.candidate
    tier, tier_detail = venue_tier_detail(cand, cfg)

    # 전문 확보를 끄면 목록만 빠르게 훑는다.
    # PDF 다운로드(최대 60초)와 요약(최대 180초)이 논문마다 붙어서,
    # "이 검색이 쓸만한가"만 보고 싶을 때는 이 둘이 시간의 대부분이다.
    fulltext_on = bool(cfg.get("summarize", {}).get("fetch_fulltext", True))

    summary: Summary | None = None
    pdf_path: str | None = None
    parsed = None
    text: str | None = None
    failure: FullTextFailure = "none"
    detail = ""
    oa = OAResult(status="unknown", source="none")

    if not fulltext_on:
        # 목록만 보는 모드. OA 여부는 값싸게 확인해 노트에 남긴다.
        check_cancel()
        emit("stage", {"stage": "oa", "title": cand.title})
        oa = oa_checker.check(cand, cfg)
        failure, detail = "skipped", "전문 확보를 끈 상태로 실행함"
        note_path = write_note(
            candidate=cand, verification=vr, oa=oa, summary=None,
            pdf_local_path=None, cfg=cfg,
            venue_tier=tier, venue_tier_detail=tier_detail,
        )
        return NoteRecord(
            candidate=cand, verification=vr, oa=oa, summary=None,
            note_path=note_path, venue_tier=tier, pdf_local_path=None,
            fulltext_failure=failure, fulltext_detail=detail,
        )

    # --- 1) CORE: 텍스트 직접 확보 ------------------------------------------
    check_cancel()
    if core_client is not None and core_client.enabled:
        emit("stage", {"stage": "core", "title": cand.title})
        core_text, core_pdf = core_client.fetch(cand)
        if core_text:
            text = core_text
            oa = OAResult(status="free", pdf_url=core_pdf, source="core-fulltext",
                          full_text=core_text)
            emit("log", {"text": f"CORE에서 전문 텍스트 확보 ({len(core_text):,}자) — PDF 단계 생략"})
        elif core_pdf and not cand.oa_pdf_url:
            cand.oa_pdf_url = core_pdf   # OA 확인 단계에서 쓰이도록

    # --- 2) OA 링크 → PDF ----------------------------------------------------
    if text is None:
        check_cancel()
        emit("stage", {"stage": "oa", "title": cand.title})
        oa = oa_checker.check(cand, cfg)

        if not oa.is_free:
            failure, detail = "no_oa_link", f"무료 사본을 찾지 못함 (OA: {oa.status})"
            emit("log", {"text": f"전문 미확보 — {detail}", "level": "warn"})
        else:
            safe = (cand.doi or cand.arxiv_id or cand.title)[:80]
            fname = "".join(c if c.isalnum() else "_" for c in safe) + ".pdf"
            emit("stage", {"stage": "fetch", "title": cand.title})
            try:
                pdf_path = fetch_pdf(oa.pdf_url, str(pdf_dir / fname))
            except NotAPdfError as e:
                failure, detail = "not_a_pdf", str(e)
                emit("log", {"text": f"PDF가 아님 — {e}", "level": "warn"})
            except PdfDownloadError as e:
                failure, detail = "download_failed", str(e)
                emit("log", {"text": f"다운로드 실패 — {e}", "level": "warn"})

            if pdf_path:
                check_cancel()
                emit("stage", {"stage": "parse", "title": cand.title})
                try:
                    parsed = parse_pdf(pdf_path)
                    text = build_paged_text(
                        parsed,
                        max_chars=int(cfg.get("summarize", {}).get("max_input_chars", 60000)),
                    )
                except ScannedPdfError as e:
                    failure, detail = "scanned_or_empty", str(e)
                    emit("log", {"text": f"텍스트 없음(스캔본) — {e}", "level": "warn"})
                except PdfParseError as e:
                    failure, detail = "parse_failed", str(e)
                    emit("log", {"text": f"PDF 해석 실패 — {e}", "level": "warn"})

    # --- 3) 요약 -------------------------------------------------------------
    if text:
        check_cancel()
        if llm_state is not None and llm_state.get("summarize_down"):
            failure, detail = "summarize_failed", "LLM을 쓸 수 없어 요약을 건너뜀"
            emit("log", {"text": "LLM 불가 — 요약 건너뜀 (전문은 확보됨)", "level": "warn"})
            text = None
        else:
            emit("stage", {"stage": "summarize", "title": cand.title})
        try:
            if text is None:
                raise _SkipSummary()
            summary = summarize(text, cfg)
            # parsed가 있으면 페이지 단위로 더 엄격하게 대조한다.
            summary = (verify_quotes(summary, paper=parsed) if parsed is not None
                       else verify_quotes(summary, source_text=text))
            emit("log", {
                "text": f"요약 완료 — 인용 {summary.verified_quote_count}/"
                        f"{len(summary.quotes)} 원문 대조 통과"
            })
        except _SkipSummary:
            summary = None
        except (LLMError, EmptyTextError) as e:
            failure, detail = "summarize_failed", str(e)
            emit("log", {"text": f"요약 실패 — {e}", "level": "warn"})
            summary = None
            # LLM 자체가 죽은 경우, 이후 논문마다 3회씩 재시도하면
            # 후보 40건에 수 분을 재시도에만 쓴다. 한 번 실패하면 접는다.
            if isinstance(e, LLMError) and llm_state is not None:
                llm_state["summarize_down"] = True

    # --- 4) 전문이 없으면 초록으로라도 정리한다 --------------------------------
    #
    # **전문 기반 요약과 절대 섞지 않는다.** depth가 abstract_only로 남고,
    # 노트와 UI가 이를 눈에 띄게 표시한다. 초록에는 방법 상세가 거의 없어서
    # 인용하거나 방법을 논할 근거로는 쓸 수 없다 — 논문 선별용이다.
    if summary is None and cand.abstract:
        use_abs = bool(cfg.get("summarize", {}).get("abstract_fallback", True))
        if use_abs and not (llm_state or {}).get("summarize_down"):
            check_cancel()
            emit("stage", {"stage": "abstract", "title": cand.title})
            try:
                summary = summarize_abstract(cand.abstract, cfg)
                summary = verify_quotes(summary, source_text=cand.abstract)
                ok, tot = summary.verified_quote_count, len(summary.quotes)
                note = " · 방법 정보 없음" if summary.method_is_missing else ""
                emit("log", {
                    "text": f"초록 기반 정리 완료 — 인용 {ok}/{tot} 대조{note} "
                            f"(전문 요약 아님)"
                })
            except EmptyTextError as e:
                emit("log", {"text": f"초록이 부족해 정리하지 않음 — {e}", "level": "warn"})
            except LLMError as e:
                emit("log", {"text": f"초록 정리 실패 — {e}", "level": "warn"})
                if llm_state is not None:
                    llm_state["summarize_down"] = True

    note_path = write_note(
        candidate=cand, verification=vr, oa=oa, summary=summary,
        pdf_local_path=pdf_path, cfg=cfg,
        venue_tier=tier, venue_tier_detail=tier_detail,
    )
    return NoteRecord(
        candidate=cand, verification=vr, oa=oa, summary=summary,
        note_path=note_path, venue_tier=tier, pdf_local_path=pdf_path,
        fulltext_failure=failure, fulltext_detail=detail[:300],
    )


# --- 이벤트 페이로드 (브라우저가 그대로 렌더할 수 있는 형태) --------------------


def _lookups(vr: VerificationResult) -> list[dict]:
    return [
        {"index": lk.index_name, "outcome": lk.outcome, "detail": lk.detail}
        for lk in vr.lookups
    ]


def _paper_payload(rec: NoteRecord, vr: VerificationResult) -> dict:
    c = rec.candidate
    s = rec.summary
    return {
        "status": "verified",
        "title": c.title,
        "authors": c.authors,
        "year": c.year,
        "venue": c.venue,
        "doi": c.doi,
        "arxiv_id": c.arxiv_id,
        "url": c.url,
        "tier": rec.venue_tier,
        "reason": vr.reason,
        "lookups": _lookups(vr),
        "oa_status": rec.oa.status,
        "summary_depth": s.depth if s else "no_summary",
        "note_path": rec.note_path,
        "method_missing": bool(s and s.method_is_missing),
        "quotes_total": len(s.quotes) if s else 0,
        "quotes_verified": s.verified_quote_count if s else 0,
        "sections": (
            {
                "배경": s.background, "방법": s.method, "핵심 결과": s.key_results_md,
                "한계": s.limitations, "관련 연구": s.related_work,
            }
            if s and s.depth == "full_text" else {}
        ),
        "quotes": (
            [
                {"page": q.page, "quote": q.quote, "verified": q.verified, "ratio": q.match_ratio}
                for q in s.quotes
            ]
            if s else []
        ),
    }


def _reject_payload(vr: VerificationResult) -> dict:
    c = vr.candidate
    return {
        "status": vr.status,
        "title": c.title,
        "authors": c.authors,
        "year": c.year,
        "venue": c.venue,
        "doi": c.doi,
        "arxiv_id": c.arxiv_id,
        "url": c.url,
        "reason": vr.reason,
        "lookups": _lookups(vr),
        "sections": {},
        "quotes": [],
    }

def resummarize_from_url(
    doi: str, pdf_source: str, cfg: dict, on_event: EventFn | None = None,
) -> NoteRecord | None:
    """구글 스칼라 등에서 직접 찾은 PDF로 재시도한다.

    전제: 이 DOI는 이전 실행에서 이미 [3] 존재 검증을 통과했다.
    그러니 검증은 다시 안 하고, DOI로 메타데이터만 다시 가져온 뒤
    곧장 [6](텍스트 추출)~[8](저장)로 간다.
    pdf_source가 http(s)로 시작하면 다운로드하고, 아니면 이미 로컬에
    있는 파일 경로로 취급한다(직접 받아둔 PDF를 바로 써도 됨).
    """
    emit = on_event or _noop
    cache_cfg = cfg.get("cache", {})
    cache = (
        LookupCache(cache_cfg.get("path", "~/.cache/paper-agent/lookup.db"),
                    ttl_days=int(cache_cfg.get("ttl_days", 90)))
        if cache_cfg.get("enabled", True) else NullCache()
    )
    openalex = OpenAlexClient(cache=cache, mailto=cfg.get("contact_email"))

    doi = doi.strip()
    r = openalex.http.get(f"{OpenAlexClient.BASE}/https://doi.org/{doi}")
    if not r.ok or not isinstance(r.json_body, dict):
        emit("log", {"text": f"OpenAlex에서 DOI 조회 실패: {doi}", "level": "error"})
        cache.close()
        return None
    cand = OpenAlexClient._to_candidate(r.json_body)

    # 재시도는 이미 만들어진 세션 폴더에 덧쓰는 것이 아니라, 논문 하나만
    # 다시 처리한다. 세션 폴더를 새로 파지 않고 vault 최상위에 저장한다
    # (`_session_dir`이 없으면 `session_root`가 vault를 가리킨다).
    pdf_dir = Path(cfg.get("paths", {}).get("pdf_dir", "./downloads")).expanduser()
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if pdf_source.startswith(("http://", "https://")):
        safe = (cand.doi or cand.title)[:80]
        fname = "".join(c if c.isalnum() else "_" for c in safe) + ".pdf"
        try:
            pdf_path = fetch_pdf(pdf_source, str(pdf_dir / fname))
        except (NotAPdfError, PdfDownloadError) as e:
            emit("log", {"text": f"PDF 받기 실패: {e}", "level": "error"})
            cache.close()
            return None
    else:
        pdf_path = pdf_source  # 이미 로컬에 받아둔 파일

    try:
        parsed = parse_pdf(pdf_path)
    except (ScannedPdfError, PdfParseError) as e:
        emit("log", {"text": f"텍스트 추출 실패: {e}", "level": "error"})
        cache.close()
        return None

    text = build_paged_text(parsed, max_chars=int(cfg.get("summarize", {}).get("max_input_chars", 60000)))
    try:
        summary = summarize(text, cfg)
        summary = verify_quotes(summary, paper=parsed)
    except (LLMError, EmptyTextError) as e:
        emit("log", {"text": f"요약 실패: {e}", "level": "error"})
        cache.close()
        return None

    vr = VerificationResult(
        candidate=cand, status="verified",
        reason="수동으로 확보한 PDF로 재시도 (존재는 이전 실행에서 이미 검증됨)",
    )
    oa = OAResult(status="free", pdf_url=pdf_source, source="manual")
    tier, tier_detail = venue_tier_detail(cand, cfg)

    note_path = write_note(
        candidate=cand, verification=vr, oa=oa, summary=summary,
        pdf_local_path=pdf_path, cfg=cfg, venue_tier=tier, venue_tier_detail=tier_detail,
    )
    emit("log", {"text": f"요약 완료 — 노트: {note_path}"})
    cache.close()
    return NoteRecord(candidate=cand, verification=vr, oa=oa, summary=summary,
                       note_path=note_path, venue_tier=tier, pdf_local_path=pdf_path)