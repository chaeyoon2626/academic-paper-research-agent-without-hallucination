"""[5] 오픈 액세스 확인.

순서: OpenAlex의 open_access 필드 → (DOI 있으면) Unpaywall 보조 조회
      → arXiv ID가 있으면 무조건 free.

free가 아니면 이 논문은 [6]~[7]을 건너뛰고 "요약 불가"로 처리된다.
그 판단은 호출부(main.py) 책임이다. 이 모듈은 사실만 보고한다.
"""

from __future__ import annotations

import logging

from core.http_client import IndexUnavailable, ThrottledClient
from core.indexes import clean_doi
from core.models import OAResult, PaperCandidate

log = logging.getLogger(__name__)

UNPAYWALL_BASE = "https://api.unpaywall.org/v2"


class OAChecker:
    def __init__(self, cfg: dict | None = None, abort=None):
        cfg = cfg or {}
        self.email = cfg.get("contact_email")
        self.use_unpaywall = bool(cfg.get("oa", {}).get("use_unpaywall", True)) and bool(self.email)
        self.http = ThrottledClient("unpaywall", min_interval=0.2, mailto=self.email, abort=abort)
        self.http_s2 = ThrottledClient("semanticscholar", min_interval=1.1, abort=abort)   # ← 추가

    def check(self, candidate: PaperCandidate, cfg: dict | None = None) -> OAResult:
        # 1) 검색 단계에서 이미 OA PDF URL을 받았으면 그대로 쓴다
        if candidate.oa_pdf_url:
            return OAResult(status="free", pdf_url=candidate.oa_pdf_url, source="openalex")

        # 2) arXiv ID가 있으면 항상 free
        if candidate.arxiv_id:
            return OAResult(status="free", pdf_url=f"https://arxiv.org/pdf/{candidate.arxiv_id}", source="arxiv")

        doi = clean_doi(candidate.doi)

        # 3) Unpaywall 보조 조회
        if self.use_unpaywall and doi:
            try:
                r = self.http.get(f"{UNPAYWALL_BASE}/{doi}", params={"email": self.email})
            except IndexUnavailable as e:
                log.info("Unpaywall 조회 불가: %s", e)
            else:
                if r.ok and isinstance(r.json_body, dict):
                    body = r.json_body
                    loc = body.get("best_oa_location") or {}
                    pdf = loc.get("url_for_pdf") or loc.get("url")
                    if body.get("is_oa") and pdf:
                        return OAResult(status="free", pdf_url=pdf, source="unpaywall")

        # 4) Unpaywall이 놓친 사본 — Semantic Scholar가 따로 색인해둔
        #    저자 홈페이지/리포지토리 사본이 있는지 한 번 더 확인
        if doi:
            try:
                r = self.http_s2.get(
                    f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                    params={"fields": "openAccessPdf"},
                )
                if r.ok and isinstance(r.json_body, dict):
                    pdf = (r.json_body.get("openAccessPdf") or {}).get("url")
                    if pdf:
                        return OAResult(status="free", pdf_url=pdf, source="semanticscholar")
                    log.warning("Semantic Scholar: 조회는 됐는데 무료 사본 없음 — %s", doi)
                else:
                    log.warning("Semantic Scholar 응답 이상 (status=%s) — %s", r.status_code, doi)
            except IndexUnavailable as e:
                log.warning("Semantic Scholar 조회 실패 — %s (%s)", doi, e)

        # 5) 알 수 없음. paid로 단정하지 않는다.
        return OAResult(status="unknown", source="none")


_default: OAChecker | None = None


def check_oa(candidate: PaperCandidate, cfg: dict) -> OAResult:
    """기획서 §6 시그니처."""
    global _default
    if _default is None:
        _default = OAChecker(cfg)
    return _default.check(candidate, cfg)
