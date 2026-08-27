"""[3] 존재 검증 — 이 프로젝트의 존재 이유.

## 판정 규칙 (기획서 §4-1 + ARS 교차 인덱스 패턴)

식별자가 **있는** 경우 — DOI는 OpenAlex + Crossref, arXiv ID는 arXiv에 조회:

    1. 어느 하나라도 `mismatch`  → not_found
       (식별자는 실존하는데 제목/저자/연도가 다르다. 진짜 DOI를 가져다
        붙인 위조가 이 패턴이고, 단순 부재보다 강한 날조 신호다.)
    2. 아니고 어느 하나라도 `match` → verified
    3. 아니고 어느 하나라도 `absent` → not_found
    4. 전부 `unavailable`          → uncertain

3번보다 2번이 먼저인 이유: DOI가 DataCite에 등록되면 Crossref는 404를 준다.
한 인덱스의 부재가 다른 인덱스의 확인을 이기면 안 된다.

식별자가 **없는** 경우 (오래된 논문, 인문학, 비영어권):

    제목 유사도 + 저자 + 연도가 모두 맞으면 verified, 아니면 **uncertain**.
    여기서 not_found를 절대 쓰지 않는다.

## precision-over-recall

마지막 규칙이 ARS가 v3.11.0에서 명시적으로 문서화한 트레이드오프다. 색인
누락과 실존하지 않음을 구분할 수 없을 때는 "확인 못 함"으로 남긴다.
반대로 하면 — 못 찾은 걸 전부 가짜로 처리하면 — 비영어권·인문학·오래된
논문이 몰살당한다. **이 도구는 가짜를 잡는 도구지, 색인 커버리지를 진리로
착각하는 도구가 아니다.**

## API 장애 ≠ 논문 없음

`unavailable`을 `not_found`로 흡수하면, 네트워크가 끊긴 날 모든 논문이
날조로 판정된다. 그래서 `core/http_client.py`의 장애 래치가 여기까지 이어진다.
"""

from __future__ import annotations

import logging

from core.indexes import ArxivClient, CrossrefClient, OpenAlexClient
from core.models import IndexLookup, PaperCandidate, VerificationResult
from core.text_similarity import (
    TITLE_SIMILARITY_THRESHOLD,
    authors_match,
    normalize_title,
    similarity,
)

log = logging.getLogger(__name__)

__all__ = ["normalize_title", "authors_match", "verify", "Verifier"]


class Verifier:
    """인덱스 클라이언트를 들고 있는 검증기. 클라이언트 재사용 = 스로틀/캐시 공유."""

    def __init__(
        self,
        cfg: dict | None = None,
        openalex: OpenAlexClient | None = None,
        crossref: CrossrefClient | None = None,
        arxiv: ArxivClient | None = None,
    ):
        cfg = cfg or {}
        mailto = cfg.get("contact_email")
        self.cfg = cfg
        self.openalex = openalex or OpenAlexClient(mailto=mailto)
        self.crossref = crossref or CrossrefClient(mailto=mailto)
        self.arxiv = arxiv or ArxivClient()
        self.threshold = float(
            cfg.get("verify", {}).get("title_similarity_threshold", TITLE_SIMILARITY_THRESHOLD)
        )
        # fast: 한 인덱스가 확인하면 멈춤 (기본). strict: 전부 조회해 교차 검증.
        self.fast_mode = str(cfg.get("verify", {}).get("mode", "fast")).lower() != "strict"

    # -- 메인 -----------------------------------------------------------------

    def verify(self, candidate: PaperCandidate) -> VerificationResult:
        if candidate.has_identifier():
            return self._verify_by_identifier(candidate)
        return self._verify_without_identifier(candidate)

    # -- 식별자 있음 -----------------------------------------------------------

    def _verify_by_identifier(self, cand: PaperCandidate) -> VerificationResult:
        """식별자로 인덱스에 조회.

        ## 속도

        인덱스마다 예의상 지켜야 하는 최소 간격이 다르다.
        OpenAlex 0.15초, Crossref 0.5초, arXiv 3초. 전부 부르면 논문 1건에
        3.65초를 대기만 한다.

        그래서 **싼 것부터 부르고, 확인되면 멈춘다**(fast 모드, 기본값).
        OpenAlex가 `match`를 주면 그 시점에 이미 "식별자가 실존하고 제목·저자·
        연도가 실제 색인 레코드와 일치한다"가 증명된 것이라 더 물어볼 필요가 없다.
        위조 DOI였다면 OpenAlex 자신이 `mismatch`를 냈을 것이다.

        `verify.mode: strict`로 두면 전부 조회해 교차 검증한다. 인덱스 간
        불일치까지 잡고 싶을 때 쓰지만, 논문당 3초 이상 더 걸린다.
        """
        lookups: list[IndexLookup] = []

        # 싼 순서대로. arXiv(3초)는 마지막.
        plan: list = []
        if cand.doi:
            plan.append(self.openalex)
            plan.append(self.crossref)
        if cand.arxiv_id:
            plan.append(self.arxiv)

        for client in plan:
            lk = client.lookup(cand)
            lookups.append(lk)

            # mismatch는 즉시 확정 — 더 볼 것 없다 (가장 강한 날조 신호)
            if lk.outcome == "mismatch":
                break
            # fast 모드: 한 곳에서 확인되면 나머지는 생략
            if self.fast_mode and lk.outcome == "match":
                break

        return self._judge(cand, lookups)

    def _judge(self, cand: PaperCandidate, lookups: list[IndexLookup]) -> VerificationResult:
        """조회 결과들을 판정 규칙에 넣는다. 규칙은 모듈 상단 주석 참조."""
        mismatches = [lk for lk in lookups if lk.outcome == "mismatch"]
        matches = [lk for lk in lookups if lk.outcome == "match"]
        absents = [lk for lk in lookups if lk.outcome == "absent"]

        best_sim = max((lk.title_similarity or 0.0 for lk in lookups), default=0.0)

        # 규칙 1 — 메타데이터 모순이 가장 강한 날조 신호
        if mismatches:
            names = ", ".join(lk.index_name for lk in mismatches)
            return VerificationResult(
                candidate=cand,
                status="not_found",
                lookups=lookups,
                title_similarity=best_sim,
                reason=(
                    f"식별자는 실존하나 메타데이터가 어긋남 ({names}). "
                    f"{mismatches[0].detail}"
                ),
            )

        # 규칙 2 — 한 곳이라도 확인되면 통과
        if matches:
            names = ", ".join(lk.index_name for lk in matches)
            return VerificationResult(
                candidate=cand,
                status="verified",
                matched_fields=matches[0].matched_fields,
                title_similarity=best_sim,
                lookups=lookups,
                reason=f"{names}에서 식별자+저자+연도 일치 확인",
            )

        # 규칙 3 — 조회는 됐는데 전부 "없다"
        if absents:
            names = ", ".join(lk.index_name for lk in absents)
            return VerificationResult(
                candidate=cand,
                status="not_found",
                lookups=lookups,
                reason=f"식별자가 {names}에 존재하지 않음",
            )

        # 규칙 4 — 전부 unavailable
        return VerificationResult(
            candidate=cand,
            status="uncertain",
            lookups=lookups,
            reason="인덱스에 도달하지 못해 판정 불가 (논문이 없다는 뜻이 아님)",
        )

    # -- 식별자 없음 -----------------------------------------------------------

    def _verify_without_identifier(self, cand: PaperCandidate) -> VerificationResult:
        """제목으로 OpenAlex를 뒤져 같은 논문을 찾는다."""
        hits = self.openalex.search(cand.title, limit=5)

        if not hits:
            return VerificationResult(
                candidate=cand,
                status="uncertain",
                lookups=[IndexLookup("openalex", "unavailable", detail="제목 검색 결과 없음")],
                reason=(
                    "식별자가 없고 제목 검색으로도 확인되지 않음. "
                    "미색인 가능성이 있어 not_found로 단정하지 않음"
                ),
            )

        scored = sorted(hits, key=lambda h: similarity(cand.title, h.title), reverse=True)
        best = scored[0]
        sim = similarity(cand.title, best.title)

        title_ok = sim >= self.threshold
        author_ok = authors_match(cand.authors, best.authors)
        authors_unknown = not cand.authors or not best.authors
        year_ok = (
            cand.year is None
            or best.year is None
            or abs(int(cand.year) - int(best.year)) <= 1
        )

        matched = []
        if title_ok:
            matched.append("title")
        if author_ok:
            matched.append("authors")
        if year_ok and cand.year and best.year:
            matched.append("year")

        # 식별자가 없으므로 저자는 반드시 확인되어야 한다.
        # (제목만으로는 동명 논문/후속 논문과 구분이 안 된다)
        if title_ok and author_ok and year_ok:
            lookup = IndexLookup(
                "openalex", "match", matched_fields=matched, title_similarity=sim,
                detail=f"제목 유사도 {sim:.2f}, 저자 일치",
            )
            # 검색으로 찾은 식별자를 후보에 채워 넣는다 ([5] 전문 확보에 필요)
            if best.doi and not cand.doi:
                cand.doi = best.doi
            if best.oa_pdf_url and not cand.oa_pdf_url:
                cand.oa_pdf_url = best.oa_pdf_url
            if best.venue and not cand.venue:
                cand.venue = best.venue

            return VerificationResult(
                candidate=cand,
                status="verified",
                matched_fields=matched,
                title_similarity=sim,
                lookups=[lookup],
                reason=f"식별자 없음 — 제목({sim:.2f})+저자+연도 조합으로 확인",
            )

        problems = []
        if not title_ok:
            problems.append(f"제목 유사도 미달({sim:.2f} < {self.threshold})")
        if not author_ok and not authors_unknown:
            problems.append("저자 불일치")
        if not year_ok:
            problems.append(f"연도 불일치({cand.year} vs {best.year})")

        return VerificationResult(
            candidate=cand,
            status="uncertain",
            matched_fields=matched,
            title_similarity=sim,
            lookups=[
                IndexLookup(
                    "openalex", "unavailable", matched_fields=matched,
                    title_similarity=sim, detail="; ".join(problems),
                )
            ],
            reason="식별자 없음 + " + ("; ".join(problems) or "확인 불충분"),
        )


# --- 기획서 시그니처 호환 래퍼 -------------------------------------------------

_default_verifier: Verifier | None = None


def verify(candidate: PaperCandidate, cfg: dict) -> VerificationResult:
    """기획서 §6 시그니처. 반복 호출 시 Verifier를 직접 만들어 재사용하는 게 낫다."""
    global _default_verifier
    if _default_verifier is None:
        _default_verifier = Verifier(cfg)
    return _default_verifier.verify(candidate)
