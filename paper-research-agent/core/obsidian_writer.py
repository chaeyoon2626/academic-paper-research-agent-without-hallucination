"""[8][9] 옵시디언 노트 저장.

frontmatter에 **요약 근거 수준을 반드시 기록한다**: `summary_depth: full_text | no_summary`.
이게 있어야 나중에 vault에서 `summary_depth: no_summary`로 검색해
"아직 전문을 못 구한 논문"을 한 번에 뽑을 수 있다.

원칙 3(투명한 실패 처리)에 따라 실패도 전부 노트로 남긴다.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from core.models import (
    NoteRecord,
    OAResult,
    PaperCandidate,
    SessionLogEntry,
    Summary,
    VerificationResult,
    VenueTier,
)

log = logging.getLogger(__name__)

_FAILURE_LABEL_KO = {
    "none": "요약 완료",
    "skipped": "전문 확보를 끄고 실행함 (다시 켜서 재실행하면 요약됩니다)",
    "no_oa_link": "무료 사본을 찾지 못함",
    "download_failed": "다운로드 실패 (봇 차단 가능성)",
    "not_a_pdf": "PDF가 아님 (랜딩/페이월 페이지)",
    "scanned_or_empty": "스캔본이라 텍스트가 없음",
    "parse_failed": "PDF 해석 실패",
    "summarize_failed": "요약 생성 실패",
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")


def make_session_name(question: str, when: datetime | None = None) -> str:
    """세션 폴더 이름. `2026-08-21 경영학 AI agent 활용 사례`

    날짜를 앞에 두면 탐색기와 옵시디언 파일 목록에서 시간순으로 정렬된다.
    질문이 뒤에 오므로 무엇을 찾았는지도 한눈에 보인다.
    """
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y-%m-%d_%H%M")
    label = sanitize_filename(question)[:60].strip() or "검색"
    return f"{stamp} {label}"


def session_root(cfg: dict) -> Path:
    """이번 실행의 결과가 들어갈 폴더.

    검색할 때마다 같은 폴더에 쌓이면 어느 논문이 어느 질문에서 나왔는지
    알 수 없게 된다. 그래서 실행마다 폴더를 따로 판다.

    `_session_dir`는 run_pipeline이 실행 시작 시 한 번 넣는다. 없으면
    (재시도 명령처럼 세션 밖에서 부를 때) vault 최상위를 쓴다.
    """
    vault = Path(cfg.get("obsidian", {}).get("vault_path", "./vault")).expanduser()
    sub = cfg.get("obsidian", {}).get("_session_dir")
    return (vault / sub) if sub else vault


def sanitize_filename(s: str) -> str:
    """파일명으로 쓸 수 없는 문자 제거. 옵시디언은 `#^[]|`도 싫어한다."""
    s = _ILLEGAL.sub("", s or "")
    s = re.sub(r"[#^\[\]|]", "", s)
    s = _WS.sub(" ", s).strip(" .")
    return s[:120] or "untitled"


def make_filename(candidate: PaperCandidate) -> str:
    """`저자 (연도) 제목.md` 형식."""
    first = candidate.authors[0].split()[-1] if candidate.authors else "Unknown"
    year = candidate.year or "n.d."
    return sanitize_filename(f"{first} ({year}) {candidate.title}") + ".md"


def _yaml_escape(v: str | None) -> str:
    # None은 null로. 빈 문자열로 쓰면 옵시디언 Dataview에서
    # "값이 없음"과 "빈 값"을 구분할 수 없다.
    if v is None or v == "":
        return "null"
    s = str(v).replace('"', '\\"').replace("\n", " ")
    return f'"{s}"'


def _yaml_list(items: list[str]) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(_yaml_escape(x) for x in items) + "]"


def _build_frontmatter(
    candidate: PaperCandidate,
    verification: VerificationResult,
    oa: OAResult,
    summary: Summary | None,
    venue_tier: VenueTier,
    pdf_local_path: str | None,
    keywords: list[str] | None = None,
    cites: list[str] | None = None,
    cited_by: list[str] | None = None,
) -> str:
    depth = summary.depth if summary else "no_summary"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines = [
        "---",
        f"title: {_yaml_escape(candidate.title)}",
        f"authors: {_yaml_list(candidate.authors)}",
        f"year: {candidate.year or 'null'}",
        f"venue: {_yaml_escape(candidate.venue)}",
        f"doi: {_yaml_escape(candidate.doi)}",
        f"arxiv_id: {_yaml_escape(candidate.arxiv_id)}",
        f"url: {_yaml_escape(candidate.url)}",
        "",
        "# 검증 결과 — 이 노트의 신뢰 수준",
        f"verification_status: {verification.status}",
        f"verified_by: {_yaml_list([lk.index_name for lk in verification.lookups if lk.is_positive])}",
        f"matched_fields: {_yaml_list(verification.matched_fields)}",
        f"title_similarity: {verification.title_similarity:.3f}",
        "",
        "# 요약 근거 수준 (원칙 2)",
        f"summary_depth: {depth}",
        f"oa_status: {oa.status}",
        f"pdf_local: {_yaml_escape(pdf_local_path)}",
        "",
        f"venue_tier: {venue_tier}",
        "",
        "# 그래프 연결 (Dataview로 질의 가능)",
        f"keywords: {_yaml_list(keywords or [])}",
        f"cites: {_yaml_list(cites or [])}",
        f"cited_by: {_yaml_list(cited_by or [])}",
        "",
        f"created: {now}",
        "tags: [paper, " + f"verify/{verification.status}, summary/{depth}, tier/{venue_tier}]",
        "---",
    ]
    return "\n".join(lines)


def _build_links_section(
    candidate: PaperCandidate,
    keywords: list[str] | None,
    cites: list[str] | None,
    cited_by: list[str] | None,
) -> list[str]:
    """옵시디언 그래프의 엣지를 만드는 위키링크 모음.

    frontmatter의 배열은 Dataview 질의용이고, 그래프 뷰는 **본문의 위키링크**만
    엣지로 그린다. 그래서 같은 정보를 여기 한 번 더 쓴다.
    """
    out: list[str] = []
    if not (keywords or cites or cited_by or candidate.venue):
        return out

    out.append("\n## 연결\n")

    if keywords:
        out.append("**주제**  " + "  ".join(f"[[{k}]]" for k in keywords) + "  ")
    if candidate.venue:
        out.append(f"**게재처**  [[{sanitize_filename(candidate.venue)}]]  ")

    if cites:
        out.append("\n**이 논문이 인용한 (수집된 것 중)**")
        for t in cites:
            out.append(f"- [[{t}]]")
    if cited_by:
        out.append("\n**이 논문을 인용한 (수집된 것 중)**")
        for t in cited_by:
            out.append(f"- [[{t}]]")
    out.append("")
    return out


def _build_body(
    candidate: PaperCandidate,
    verification: VerificationResult,
    oa: OAResult,
    summary: Summary | None,
    venue_tier_detail: str = "",
) -> str:
    out: list[str] = [f"\n# {candidate.title}\n"]

    # 메타
    authors = ", ".join(candidate.authors) if candidate.authors else "저자 정보 없음"
    out.append(f"**저자**: {authors}  ")
    out.append(f"**연도**: {candidate.year or '미상'}  ")
    out.append(f"**게재처**: {candidate.venue or '미상'}"
               + (f" ({venue_tier_detail})" if venue_tier_detail else "") + "  ")
    if candidate.doi:
        out.append(f"**DOI**: [{candidate.doi}](https://doi.org/{candidate.doi})  ")
    if candidate.arxiv_id:
        out.append(f"**arXiv**: [{candidate.arxiv_id}](https://arxiv.org/abs/{candidate.arxiv_id})  ")
    if candidate.url:
        out.append(f"**링크**: {candidate.url}  ")

    # 검증 내역
    out.append("\n## 검증 내역\n")
    icon = {"verified": "✅", "uncertain": "❓", "not_found": "❌",
            "irrelevant": "🚫"}.get(verification.status, "❓")
    out.append(f"{icon} **{verification.status}** — {verification.reason}\n")
    if verification.lookups:
        out.append("| 인덱스 | 결과 | 상세 |")
        out.append("|---|---|---|")
        for lk in verification.lookups:
            out.append(f"| {lk.index_name} | `{lk.outcome}` | {lk.detail} |")
        out.append("")

    # 요약
    out.append("\n## 요약\n")
    if summary is None or summary.depth == "no_summary":
        out.append("> [!warning] 요약 불가 — 전문을 확보하지 못함")
        out.append(f"> 오픈 액세스 상태: `{oa.status}`"
                   + (f" (출처: {oa.source})" if oa.source else ""))
        out.append(">")
        out.append("> 초록만으로는 요약하지 않습니다(원칙 2). 전문을 직접 구하신 뒤")
        out.append("> 다시 실행하거나, 위 링크에서 확인하세요.\n")
        if candidate.abstract:
            out.append("<details><summary>API가 제공한 초록 (요약 아님, 원문 그대로)</summary>\n")
            out.append(f"\n{candidate.abstract}\n")
            out.append("\n</details>\n")
    elif summary.depth == "abstract_only":
        # 이 배너가 없으면 읽는 사람이 전문 요약으로 착각한다.
        out.append("> [!warning] 초록만 보고 정리했습니다 — 전문 요약이 아닙니다")
        out.append("> 초록에는 방법 상세(표본, 측정, 분석 기법)가 거의 없습니다.")
        if summary.method_is_missing:
            out.append("> **이 논문의 방법은 초록에 나오지 않아 비어 있습니다.**")
        out.append("> 논문을 고르는 데는 쓸 수 있지만, **인용하거나 방법을 논할 근거로는")
        out.append("> 쓸 수 없습니다.** 아래 '전문 확인이 필요한 부분'을 보세요.\n")

        for heading, text in (
            ("배경", summary.background),
            ("방법", summary.method),
            ("핵심 결과", summary.key_results_md),
            ("결론", summary.conclusion),
        ):
            if text and text.strip():
                out.append(f"### {heading}\n")
                out.append(text.strip() + "\n")

        if summary.open_questions.strip():
            out.append("### 전문 확인이 필요한 부분\n")
            out.append(summary.open_questions.strip() + "\n")

    else:
        for heading, text in (
            ("배경", summary.background),
            ("방법", summary.method),
            ("핵심 결과", summary.key_results_md),
            ("한계", summary.limitations),
            ("관련 연구", summary.related_work),
            ("결론", summary.conclusion),
        ):
            if text and text.strip():
                out.append(f"### {heading}\n")
                out.append(text.strip() + "\n")

    # 인용 대조는 두 방식 모두에 적용된다
    if summary is not None and summary.depth != "no_summary":
        if summary.quotes:
            out.append("\n## 인용 대조 결과\n")
            ok, total = summary.verified_quote_count, len(summary.quotes)
            out.append(f"원문 대조: **{ok}/{total}** 통과\n")
            out.append("| 페이지 | 인용 | 대조 |")
            out.append("|---|---|---|")
            for q in summary.quotes:
                mark = "✅" if q.verified else "⚠ 실패"
                quote = q.quote.replace("|", "\\|")
                if len(quote) > 120:
                    quote = quote[:120] + "…"
                page = f"p.{q.page}" if q.page else "—"
                out.append(f"| {page} | {quote} | {mark} ({q.match_ratio}) |")
            if summary.failed_quote_count:
                out.append("")
                out.append("> [!caution] 대조에 실패한 인용이 있습니다")
                out.append("> 지우지 않고 남겨둡니다(원칙 3). LLM이 지어냈거나, "
                           "PDF 추출 노이즈로 문자열이 어긋난 것일 수 있습니다.")
            out.append("")

    return "\n".join(out)


def write_note(
    candidate: PaperCandidate,
    verification: VerificationResult,
    oa: OAResult,
    summary: Summary | None,
    pdf_local_path: str | None,
    cfg: dict,
    venue_tier: VenueTier = "unknown",
    venue_tier_detail: str = "",
    keywords: list[str] | None = None,
    cites: list[str] | None = None,
    cited_by: list[str] | None = None,
) -> str:
    """frontmatter + 본문을 만들어 vault에 .md 저장. 경로 반환.

    keywords/cites/cited_by는 그래프 엣지를 만든다. 1차 저장 때는 비어 있고,
    세션이 끝난 뒤 `enrich_note`가 채운다 — 논문 간 인용 관계는 전부 모아봐야
    알 수 있기 때문이다.
    """
    dest_dir = session_root(cfg) / cfg.get("obsidian", {}).get("papers_dir", "papers")
    dest_dir.mkdir(parents=True, exist_ok=True)

    path = dest_dir / make_filename(candidate)
    if path.exists() and not cfg.get("obsidian", {}).get("overwrite", False):
        stem, suffix = path.stem, path.suffix
        i = 2
        while path.exists():
            path = dest_dir / f"{stem} ({i}){suffix}"
            i += 1

    content = (
        _build_frontmatter(candidate, verification, oa, summary, venue_tier,
                           pdf_local_path, keywords, cites, cited_by)
        + _build_body(candidate, verification, oa, summary, venue_tier_detail)
        + "\n".join(_build_links_section(candidate, keywords, cites, cited_by))
    )
    path.write_text(content, encoding="utf-8")
    log.info("노트 저장: %s", path)
    return str(path)


def write_session_log(entries: list[SessionLogEntry], cfg: dict) -> str:
    """`sessions/`에 실행 로그 저장."""
    dest_dir = session_root(cfg)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    path = dest_dir / f"_실행 로그 {ts}.md"

    lines = [
        "---",
        f"created: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "tags: [session-log]",
        "---",
        "",
        f"# 실행 로그 {ts}",
        "",
        "| 쿼리 | 후보 | 검증됨 | 불확실 | 없음 | 모델 |",
        "|---|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| {e.query} | {e.candidates_found} | {e.verified_count} | "
            f"{e.uncertain_count} | {e.not_found_count} | {e.model_used} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def write_session_moc(
    question: str,
    records: list[NoteRecord],
    uncertain: list[VerificationResult],
    not_found: list[VerificationResult],
    cfg: dict,
    irrelevant: list[VerificationResult] | None = None,
    failure_tally: dict[str, int] | None = None,
    seed_papers: list | None = None,
    seed_vocabulary: list[str] | None = None,
) -> str:
    """[9] 세션 인덱스 노트(MOC). 성공/불확실/실패를 전부 보여준다 — 원칙 3."""
    dest_dir = session_root(cfg)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 밑줄로 시작해 파일 목록 맨 위에 오게 한다 — 세션 폴더의 표지 역할
    path = dest_dir / f"_{sanitize_filename(question)[:60] or '검색'}.md"

    summarized = [r for r in records if r.summary and r.summary.depth == "full_text"]
    abstract_only = [r for r in records if r.summary and r.summary.depth == "abstract_only"]
    no_summary = [r for r in records
                  if not (r.summary and r.summary.depth in ("full_text", "abstract_only"))]

    L = [
        "---",
        f"question: {_yaml_escape(question)}",
        f"created: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "tags: [session-moc]",
        "---",
        "",
        f"# {question}",
        "",
        "## 집계",
        "",
        f"- 검증 통과: **{len(records)}건**",
        f"  - 전문 기반 요약: **{len(summarized)}건**",
        f"  - 초록 기반 정리: **{len(abstract_only)}건** (전문 요약 아님)",
        f"  - 요약 없음: **{len(no_summary)}건**",
        f"- 검증 불확실: **{len(uncertain)}건**",
        f"- 존재 확인 실패: **{len(not_found)}건**",
        f"- 질문과 무관: **{len(irrelevant or [])}건**",
        "",
    ]

    # 어떤 논문에서 출발했는지 남긴다. 결과가 이상할 때 시드가 원인인지
    # 판단할 수 있어야 한다.
    if seed_papers:
        L += ["## 출발점(시드) 논문", ""]
        for c in seed_papers:
            doi = f" · [{c.doi}](https://doi.org/{c.doi})" if c.doi else ""
            L.append(f"- {c.title} ({c.year or 'n.d.'}){doi}")
        if seed_vocabulary:
            L += ["", f"**검색에 쓴 분야 용어**: {', '.join(seed_vocabulary)}"]
        L.append("")

    # 전문을 못 구한 이유를 원인별로 보여준다. 이게 있어야 다음에 뭘
    # 고쳐야 할지 알 수 있다 — 소스 부족인지, 다운로드 문제인지.
    if failure_tally:
        L += ["## 전문 확보 실패 원인", "",
              "| 원인 | 건수 |", "|---|---|"]
        for reason, n in sorted(failure_tally.items(), key=lambda x: -x[1]):
            L.append(f"| {reason} | {n} |")
        L += ["",
              "> 무료 사본 없음이 대부분이면 검색 소스를 늘려야 합니다.",
              "> PDF가 아님이 대부분이면 랜딩 페이지 처리를 손봐야 합니다.",
              "> 다운로드 실패가 대부분이면 리포지토리가 봇을 차단하는 중입니다.",
              ""]

    if summarized:
        L += ["## ✅ 전문 기반 요약 완료", ""]
        for r in summarized:
            tier = f" `{r.venue_tier}`" if r.venue_tier != "unknown" else ""
            q = r.summary.quotes if r.summary else []
            qinfo = f" — 인용 {sum(1 for x in q if x.verified)}/{len(q)} 대조" if q else ""
            L.append(f"- [[{Path(r.note_path).stem}]]{tier}{qinfo}")
        L.append("")

    if abstract_only:
        L += ["## 📄 초록 기반 정리 (전문 요약 아님)", "",
              "*전문을 못 구해 초록만 보고 정리했습니다. 방법 상세가 대개 빠져 있어 "
              "**논문 선별용**이지, 인용하거나 방법을 논할 근거로는 쓸 수 없습니다.*", ""]
        for r in abstract_only:
            q = r.summary.quotes if r.summary else []
            qinfo = f" — 인용 {sum(1 for x in q if x.verified)}/{len(q)} 대조" if q else ""
            warn = "  ⚠ 방법 정보 없음" if (r.summary and r.summary.method_is_missing) else ""
            L.append(f"- [[{Path(r.note_path).stem}]]{qinfo}{warn}")
            if r.candidate.doi:
                L.append(f"  `py main.py --retry-doi \"{r.candidate.doi}\" "
                         f"--retry-pdf \"<PDF 주소나 파일 경로>\"`")
        L += ["", "> 전문을 구하시면 위 명령으로 제대로 된 요약을 만들 수 있습니다.", ""]

    if no_summary:
        L += ["## ⚠ 검증됨 · 요약 없음", ""]
        for r in no_summary:
            why = _FAILURE_LABEL_KO.get(r.fulltext_failure, r.fulltext_failure)
            line = f"- [[{Path(r.note_path).stem}]] — {why}"
            if r.candidate.doi:
                line += f"  \n  `py main.py --retry-doi \"{r.candidate.doi}\" --retry-pdf \"<PDF 주소나 파일 경로>\"`"
            L.append(line)
        L += ["",
              "> 위 논문의 PDF를 직접 구하셨다면, 각 줄의 명령으로 요약만 다시 돌릴 수 있습니다.",
              ""]

    if irrelevant:
        L += ["## 🚫 질문과 무관하다고 판단됨", "",
              "*실존하는 논문이지만 이 질문과 관련이 없다고 판단해 제외했습니다. "
              "잘못 걸러진 게 있으면 알려주세요.*", ""]
        for v in irrelevant:
            y = v.candidate.year or "n.d."
            L.append(f"- {v.candidate.title} ({y}) — {v.reason}")
        L.append("")

    if uncertain:
        L += ["## ❓ 검증 불확실", "",
              "*미색인 가능성이 있어 '없음'으로 단정하지 않았습니다. 직접 확인이 필요합니다.*", ""]
        for v in uncertain:
            y = v.candidate.year or "n.d."
            L.append(f"- {v.candidate.title} ({y}) — {v.reason}")
        L.append("")

    if not_found:
        L += ["## ❌ 존재 확인 실패", "",
              "*식별자 조회가 명시적으로 실패했거나 메타데이터가 모순됩니다.*", ""]
        for v in not_found:
            y = v.candidate.year or "n.d."
            L.append(f"- ~~{v.candidate.title}~~ ({y}) — {v.reason}")
        L.append("")

    path.write_text("\n".join(L), encoding="utf-8")
    log.info("세션 MOC 저장: %s", path)
    return str(path)


# ---------------------------------------------------------------------------
# 그래프 보강 (세션이 끝난 뒤)
# ---------------------------------------------------------------------------


def enrich_note(
    note_path: str,
    keywords: list[str],
    cites: list[str],
    cited_by: list[str],
) -> bool:
    """이미 저장된 노트에 그래프 연결을 채워 넣는다.

    논문 간 인용 관계는 **전부 모아봐야** 알 수 있어서 저장 시점에는 비어 있다.
    세션이 끝난 뒤 이 함수가 frontmatter와 연결 섹션을 다시 쓴다.
    """
    p = Path(note_path)
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("노트를 읽지 못함 (%s): %s", note_path, e)
        return False

    for key, vals in (("keywords", keywords), ("cites", cites), ("cited_by", cited_by)):
        text = re.sub(
            rf"^{key}: \[.*?\]$",
            f"{key}: {_yaml_list(vals)}",
            text,
            count=1,
            flags=re.MULTILINE,
        )

    # 기존 연결 섹션을 지우고 새로 붙인다 (중복 방지)
    text = re.sub(r"\n## 연결\n.*$", "", text, flags=re.DOTALL).rstrip()

    from core.models import PaperCandidate as _PC

    venue = None
    m = re.search(r'^venue: "(.*?)"$', text, re.MULTILINE)
    if m and m.group(1) != "null":
        venue = m.group(1)

    section = _build_links_section(_PC(title="", venue=venue), keywords, cites, cited_by)
    if section:
        text += "\n" + "\n".join(section)

    try:
        p.write_text(text + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("노트를 쓰지 못함 (%s): %s", note_path, e)
        return False
    return True
