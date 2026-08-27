"""CORE API 클라이언트 — 링크가 아니라 **전문 텍스트를 직접** 받아온다.

## 왜 필요한가

OpenAlex / Unpaywall / Semantic Scholar는 "PDF가 여기 있다"는 링크만 준다.
그 링크는 수백 개의 서로 다른 리포지토리·출판사 서버를 가리키고, 실제로
받아지느냐는 별개 문제다 — 봇 차단(403), 랜딩 페이지, 스캔본, 깨진 조판.

CORE는 전 세계 1만 개 이상의 기관 리포지토리를 수집해 **텍스트로 변환한 것**을
API 응답에 담아준다. 텍스트를 받으면 [5] 다운로드와 [6] 파싱을 통째로 건너뛴다.
실패할 수 있는 단계가 두 개 사라지는 셈이다.

## 경영학·사회과학에 특히 유효한 이유

기관 리포지토리에는 출판사 페이월 뒤에 있는 논문의 **저자 최종본**(author
accepted manuscript)이 올라오는 경우가 많다. Elsevier·Emerald·Wiley 비중이
큰 분야에서 이게 실질적인 우회로가 된다.

## 키

무료 발급이며 https://core.ac.uk/services/api 에서 받는다.
키가 없으면 이 클라이언트는 조용히 비활성화된다 — 없다고 파이프라인이
멈추면 안 된다.
"""

from __future__ import annotations

import logging
import os

from core.http_client import IndexUnavailable, ThrottledClient
from core.indexes import clean_doi
from core.models import PaperCandidate

log = logging.getLogger(__name__)

BASE = "https://api.core.ac.uk/v3"
MIN_TEXT_CHARS = 1500
"""이보다 짧으면 초록이나 표지만 받아온 것으로 본다.

원칙 2: 전문이 아닌 걸 전문으로 착각해 요약하면 안 된다.
"""

MAX_TEXT_CHARS = 400_000


class CoreClient:
    def __init__(self, cfg: dict | None = None, abort=None):
        cfg = cfg or {}
        core_cfg = cfg.get("core", {})
        # 키는 .env(환경변수)를 우선 본다. config.yaml에 키를 적으면
        # 실수로 커밋될 수 있다.
        self.api_key = (
            os.environ.get("CORE_API_KEY", "").strip()
            or str(core_cfg.get("api_key", "")).strip()
        )
        self.enabled = bool(core_cfg.get("enabled", True)) and bool(self.api_key)
        # 문서 기준 10초당 단건 5회. 여유 있게 2.2초.
        self.http = ThrottledClient("core", min_interval=2.2, timeout=45, abort=abort)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    # -- 응답에서 텍스트 꺼내기 ------------------------------------------------

    @staticmethod
    def _extract_text(work: dict) -> str | None:
        raw = work.get("fullText")
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if len(text) < MIN_TEXT_CHARS:
            return None
        return text[:MAX_TEXT_CHARS]

    @staticmethod
    def _extract_pdf(work: dict) -> str | None:
        return work.get("downloadUrl") or work.get("fullTextIdentifier") or None

    # -- DOI로 조회 -----------------------------------------------------------

    def fetch_by_doi(self, doi: str) -> tuple[str | None, str | None]:
        """DOI로 전문을 찾는다.

        Returns:
            (전문 텍스트 | None, PDF URL | None)
            텍스트가 없어도 PDF 링크는 줄 수 있으므로 둘 다 반환한다.
        """
        if not self.enabled:
            return None, None
        doi = clean_doi(doi) or ""
        if not doi:
            return None, None

        try:
            r = self.http.get(
                f"{BASE}/search/works",
                params={"q": f'doi:"{doi}"', "limit": 3},
                headers=self._headers(),
            )
        except IndexUnavailable as e:
            log.info("CORE 조회 불가: %s", e)
            return None, None

        if not r.ok or not isinstance(r.json_body, dict):
            return None, None

        results = r.json_body.get("results") or []
        if not isinstance(results, list):
            return None, None

        # 텍스트가 있는 레코드를 우선 고른다. 같은 논문이 여러 리포지토리에
        # 중복 수집돼 있고, 그중 일부만 전문을 갖고 있는 경우가 흔하다.
        pdf_fallback = None
        for w in results:
            if not isinstance(w, dict):
                continue
            text = self._extract_text(w)
            if text:
                log.info("CORE에서 전문 확보: %s (%d자)", doi, len(text))
                return text, self._extract_pdf(w)
            if pdf_fallback is None:
                pdf_fallback = self._extract_pdf(w)

        if pdf_fallback:
            log.info("CORE: 전문 텍스트는 없고 PDF 링크만 있음 — %s", doi)
        return None, pdf_fallback

    # -- 제목으로 조회 (DOI가 없을 때) ------------------------------------------

    def fetch_by_title(self, candidate: PaperCandidate) -> tuple[str | None, str | None]:
        if not self.enabled or not candidate.title:
            return None, None

        from core.text_similarity import titles_match

        try:
            r = self.http.get(
                f"{BASE}/search/works",
                params={"q": f'title:"{candidate.title[:200]}"', "limit": 5},
                headers=self._headers(),
            )
        except IndexUnavailable as e:
            log.info("CORE 제목 조회 불가: %s", e)
            return None, None

        if not r.ok or not isinstance(r.json_body, dict):
            return None, None

        pdf_fallback = None
        for w in r.json_body.get("results") or []:
            if not isinstance(w, dict):
                continue
            # 제목 검색은 느슨하다. 엉뚱한 논문의 전문을 가져오면
            # 그 요약은 통째로 거짓이 되므로 반드시 대조한다.
            if not titles_match(candidate.title, w.get("title") or ""):
                continue
            text = self._extract_text(w)
            if text:
                log.info("CORE에서 전문 확보(제목 매칭): %s", candidate.title[:50])
                return text, self._extract_pdf(w)
            if pdf_fallback is None:
                pdf_fallback = self._extract_pdf(w)

        return None, pdf_fallback

    def fetch(self, candidate: PaperCandidate) -> tuple[str | None, str | None]:
        """DOI 우선, 없으면 제목으로."""
        if candidate.doi:
            text, pdf = self.fetch_by_doi(candidate.doi)
            if text or pdf:
                return text, pdf
        return self.fetch_by_title(candidate)
