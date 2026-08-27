"""서지 인덱스 클라이언트 3종: OpenAlex, Crossref, arXiv.

각 클라이언트는 두 가지를 한다:
  - `search(query)`  → [2] 후보 수집
  - `lookup(...)`    → [3] 식별자로 단건 조회 (검증용)

ARS의 4-인덱스 교차 검증(Semantic Scholar + OpenAlex + Crossref + arXiv)에서
Semantic Scholar를 뺀 3종이다. S2를 뺀 이유: 무인증 레이트 리밋이 빡세서
(공유 풀 기준 초당 1건 미만) 배치 검증에서 병목이 되고, 커버리지가
OpenAlex와 크게 겹친다. 나중에 `IndexClient` 인터페이스에 맞춰 추가하면 된다.

**모든 lookup은 예외를 던지지 않고 `IndexLookup`을 반환한다.** 실패도 데이터다.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from core.cache import LookupCache, NullCache
from core.http_client import IndexUnavailable, ThrottledClient
from core.models import IndexLookup, PaperCandidate
from core.text_similarity import (
    TITLE_SIMILARITY_THRESHOLD,
    author_overlap,
    authors_match,
    similarity,
)

log = logging.getLogger(__name__)

_DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?|([a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.IGNORECASE)


def clean_doi(raw: str | None) -> str | None:
    """'https://doi.org/10.1/x', 'doi:10.1/x' → '10.1/x'"""
    if not raw:
        return None
    m = _DOI_RE.search(raw.strip())
    return m.group(0).rstrip(".,;").lower() if m else None


def clean_arxiv_id(raw: str | None) -> str | None:
    """'arXiv:2301.00234v2', 'http://arxiv.org/abs/2301.00234' → '2301.00234'"""
    if not raw:
        return None
    m = _ARXIV_RE.search(raw.strip())
    if not m:
        return None
    return (m.group(1) or m.group(3) or "").lower()


def _reconstruct_abstract(inverted: dict | None) -> str | None:
    """OpenAlex는 초록을 inverted index로 준다. 단어→위치 리스트를 되돌린다."""
    if not inverted:
        return None
    try:
        positions: list[tuple[int, str]] = []
        for word, idxs in inverted.items():
            for i in idxs:
                positions.append((i, word))
        positions.sort()
        return " ".join(w for _, w in positions) or None
    except (AttributeError, TypeError):
        return None


# =============================================================================
# OpenAlex
# =============================================================================


class OpenAlexClient:
    BASE = "https://api.openalex.org/works"

    def __init__(self, cache: LookupCache | None = None, mailto: str | None = None,
                 timeout: int = 20, abort=None):
        # OpenAlex 무인증 한도는 하루 10만 건, 초당 10건. 여유롭게 0.15s.
        self.http = ThrottledClient("openalex", min_interval=0.15, timeout=timeout, mailto=mailto, abort=abort)
        self.cache = cache or NullCache()

    # -- 파싱 -----------------------------------------------------------------

    @staticmethod
    def _to_candidate(w: dict) -> PaperCandidate:
        authors = [
            a.get("author", {}).get("display_name", "")
            for a in (w.get("authorships") or [])
            if a.get("author")
        ]
        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        oa = w.get("open_access") or {}
        best = w.get("best_oa_location") or {}
        _landing = loc.get("landing_page_url") or ""

        return PaperCandidate(
            title=w.get("display_name") or w.get("title") or "",
            authors=[a for a in authors if a],
            year=w.get("publication_year"),
            doi=clean_doi(w.get("doi")),
            arxiv_id=clean_arxiv_id(_landing) if "arxiv.org" in _landing else None,
            venue=src.get("display_name"),
            abstract=_reconstruct_abstract(w.get("abstract_inverted_index")),
            url=w.get("doi") or loc.get("landing_page_url"),
            oa_pdf_url=best.get("pdf_url") or oa.get("oa_url"),
            source_api="openalex",
        )

    # -- [2] 검색 -------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[PaperCandidate]:
        try:
            r = self.http.get(
                self.BASE,
                params={
                    "search": query,
                    "per-page": min(limit, 50),
                    "select": (
                        "id,doi,display_name,publication_year,authorships,"
                        "primary_location,open_access,best_oa_location,abstract_inverted_index"
                    ),
                },
            )
        except IndexUnavailable as e:
            log.warning("OpenAlex 검색 실패: %s", e)
            return []
        if not r.ok or not isinstance(r.json_body, dict):
            return []
        return [self._to_candidate(w) for w in (r.json_body.get("results") or [])]

    # -- [3] 검증 조회 --------------------------------------------------------

    def lookup(self, cand: PaperCandidate) -> IndexLookup:
        doi = clean_doi(cand.doi)
        if not doi:
            return IndexLookup("openalex", "unavailable", detail="DOI 없음 — 조회 불가")

        cached = self.cache.get("openalex", doi)
        if cached is not None:
            return _compare_against(cand, cached, "openalex")

        try:
            r = self.http.get(f"{self.BASE}/https://doi.org/{doi}")
        except IndexUnavailable as e:
            return IndexLookup("openalex", "unavailable", detail=str(e))

        if r.is_absent:
            # 명시적 부재. 이건 진짜 신호다.
            return IndexLookup("openalex", "absent", detail=f"DOI {doi} 미색인")
        if not r.ok or not isinstance(r.json_body, dict):
            return IndexLookup("openalex", "unavailable", detail=f"http {r.status_code}")

        found = self._to_candidate(r.json_body)
        record = {
            "title": found.title,
            "authors": found.authors,
            "year": found.year,
            "venue": found.venue,
            "oa_pdf_url": found.oa_pdf_url,
        }
        self.cache.set("openalex", doi, record)
        return _compare_against(cand, record, "openalex")


# =============================================================================
# Crossref
# =============================================================================


class CrossrefClient:
    """DOI의 공식 등록 기관. DOI 검증에서는 여기가 최종 권위다."""

    BASE = "https://api.crossref.org/works"

    def __init__(self, cache: LookupCache | None = None, mailto: str | None = None,
                 timeout: int = 20, abort=None):
        self.http = ThrottledClient("crossref", min_interval=0.5, timeout=timeout, mailto=mailto, abort=abort)
        self.cache = cache or NullCache()

    @staticmethod
    def _parse(msg: dict) -> dict:
        titles = msg.get("title") or []
        authors = []
        for a in msg.get("author") or []:
            # Crossref는 성(family)과 이름(given)을 **나눠서** 준다.
            # 그냥 이어붙이면 그 구분이 사라진다. 그러면 "Kim Minsu"에서
            # 어느 쪽이 성인지 알 수 없게 되고, 서지 내보내기가 어긋난다
            # (영어권은 뒤가 성, 한국어 로마자 표기는 앞이 성이라 추측이 불가능하다).
            # "성, 이름" 형태로 두면 구분이 문자열 안에 남는다.
            family, given = a.get("family"), a.get("given")
            if family and given:
                name = f"{family}, {given}"
            else:
                name = family or given or a.get("name") or ""
            if name:
                authors.append(name)

        year = None
        for key in ("published-print", "published-online", "issued", "created"):
            parts = (msg.get(key) or {}).get("date-parts") or []
            if parts and parts[0] and parts[0][0]:
                year = parts[0][0]
                break

        container = msg.get("container-title") or []
        return {
            "title": titles[0] if titles else "",
            "authors": authors,
            "year": year,
            "venue": container[0] if container else None,
        }

    def lookup(self, cand: PaperCandidate) -> IndexLookup:
        doi = clean_doi(cand.doi)
        if not doi:
            return IndexLookup("crossref", "unavailable", detail="DOI 없음 — 조회 불가")

        cached = self.cache.get("crossref", doi)
        if cached is not None:
            return _compare_against(cand, cached, "crossref")

        try:
            r = self.http.get(f"{self.BASE}/{doi}")
        except IndexUnavailable as e:
            return IndexLookup("crossref", "unavailable", detail=str(e))

        if r.is_absent:
            return IndexLookup("crossref", "absent", detail=f"DOI {doi} 미등록")
        if not r.ok or not isinstance(r.json_body, dict):
            return IndexLookup("crossref", "unavailable", detail=f"http {r.status_code}")

        record = self._parse(r.json_body.get("message") or {})
        self.cache.set("crossref", doi, record)
        return _compare_against(cand, record, "crossref")


# =============================================================================
# arXiv
# =============================================================================


class ArxivClient:
    """Atom XML을 반환한다. API 키 불필요."""

    BASE = "http://export.arxiv.org/api/query"
    NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    def __init__(self, cache: LookupCache | None = None, timeout: int = 20, abort=None):
        # arXiv는 공식적으로 3초 간격을 요청한다. 지키자.
        self.http = ThrottledClient("arxiv", min_interval=3.0, timeout=timeout, abort=abort)
        self.cache = cache or NullCache()

    def _parse_entries(self, xml_text: str) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.warning("arXiv XML 파싱 실패: %s", e)
            return []

        out = []
        for e in root.findall("atom:entry", self.NS):
            # 결과 0건일 때 arXiv는 더미 entry를 주기도 한다.
            id_el = e.find("atom:id", self.NS)
            title_el = e.find("atom:title", self.NS)
            if id_el is None or title_el is None:
                continue
            aid = clean_arxiv_id(id_el.text or "")
            if not aid:
                continue

            authors = [
                (n.text or "").strip()
                for n in e.findall("atom:author/atom:name", self.NS)
                if n.text
            ]
            published = e.find("atom:published", self.NS)
            year = None
            if published is not None and published.text:
                try:
                    year = int(published.text[:4])
                except ValueError:
                    pass

            pdf_url = None
            for link in e.findall("atom:link", self.NS):
                if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                    pdf_url = link.get("href")
                    break
            if not pdf_url:
                pdf_url = f"https://arxiv.org/pdf/{aid}"

            summary_el = e.find("atom:summary", self.NS)
            doi_el = e.find("arxiv:doi", self.NS)

            out.append(
                {
                    "arxiv_id": aid,
                    "title": " ".join((title_el.text or "").split()),
                    "authors": authors,
                    "year": year,
                    "abstract": " ".join((summary_el.text or "").split()) if summary_el is not None else None,
                    "pdf_url": pdf_url,
                    "doi": clean_doi(doi_el.text) if doi_el is not None else None,
                }
            )
        return out

    # -- [2] 검색 -------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[PaperCandidate]:
        try:
            r = self.http.get(
                self.BASE,
                params={
                    "search_query": f"all:{query}",
                    "start": 0,
                    "max_results": min(limit, 50),
                },
                accept="application/atom+xml",
            )
        except IndexUnavailable as e:
            log.warning("arXiv 검색 실패: %s", e)
            return []
        if not r.ok:
            return []

        return [
            PaperCandidate(
                title=d["title"],
                authors=d["authors"],
                year=d["year"],
                doi=d["doi"],
                arxiv_id=d["arxiv_id"],
                venue="arXiv (preprint)",
                abstract=d["abstract"],
                url=f"https://arxiv.org/abs/{d['arxiv_id']}",
                oa_pdf_url=d["pdf_url"],
                source_api="arxiv",
            )
            for d in self._parse_entries(r.text_body)
        ]

    # -- [3] 검증 조회 --------------------------------------------------------

    def lookup(self, cand: PaperCandidate) -> IndexLookup:
        aid = clean_arxiv_id(cand.arxiv_id)
        if not aid:
            return IndexLookup("arxiv", "unavailable", detail="arXiv ID 없음 — 조회 불가")

        cached = self.cache.get("arxiv", aid)
        if cached is not None:
            return _compare_against(cand, cached, "arxiv")

        try:
            r = self.http.get(
                self.BASE, params={"id_list": aid}, accept="application/atom+xml"
            )
        except IndexUnavailable as e:
            return IndexLookup("arxiv", "unavailable", detail=str(e))
        if not r.ok:
            return IndexLookup("arxiv", "unavailable", detail=f"http {r.status_code}")

        entries = self._parse_entries(r.text_body)
        if not entries:
            # arXiv는 없는 ID에 404가 아니라 빈 피드를 준다. 이건 명시적 부재로 본다.
            return IndexLookup("arxiv", "absent", detail=f"arXiv:{aid} 없음")

        d = entries[0]
        record = {
            "title": d["title"],
            "authors": d["authors"],
            "year": d["year"],
            "venue": "arXiv (preprint)",
            "oa_pdf_url": d["pdf_url"],
        }
        self.cache.set("arxiv", aid, record)
        return _compare_against(cand, record, "arxiv")


# =============================================================================
# 공통 비교 로직
# =============================================================================


def _compare_against(cand: PaperCandidate, record: dict, index_name: str) -> IndexLookup:
    """식별자로 찾아온 실제 레코드와 후보를 대조한다.

    기획서 §4-1: **식별자만으로는 부족하다.** 진짜 DOI를 가져다 붙이고 제목·저자를
    바꿔치기한 위조가 실제로 있기 때문에, 조회에 성공했어도 저자·연도가 어긋나면
    `mismatch`(= 부정 신호)로 처리한다.
    """
    matched: list[str] = ["identifier"]
    ref_title = record.get("title") or ""
    ref_authors = record.get("authors") or []
    ref_year = record.get("year")

    sim = similarity(cand.title, ref_title) if ref_title else 0.0
    title_ok = sim >= TITLE_SIMILARITY_THRESHOLD
    if title_ok:
        matched.append("title")

    author_ok = authors_match(cand.authors, ref_authors)
    if author_ok:
        matched.append("authors")

    # 연도는 ±1 허용: 온라인 선공개와 지면 게재 연도가 갈리는 경우가 흔하다.
    year_ok = False
    if cand.year is None or ref_year is None:
        year_ok = True  # 한쪽이 비면 연도로는 반증하지 않는다
        detail_year = "연도 정보 부족 — 판정 보류"
    else:
        year_ok = abs(int(cand.year) - int(ref_year)) <= 1
        detail_year = f"연도 {cand.year} vs {ref_year}"
        if year_ok:
            matched.append("year")

    # 저자가 양쪽 다 비어 있으면 저자로 반증할 수 없다 → 제목으로만 판정
    authors_unknown = not cand.authors or not ref_authors

    if title_ok and year_ok and (author_ok or authors_unknown):
        outcome = "match"
        detail = f"제목 유사도 {sim:.2f}, 저자 일치 {author_overlap(cand.authors, ref_authors)}명, {detail_year}"
    else:
        outcome = "mismatch"
        problems = []
        if not title_ok:
            problems.append(f"제목 불일치({sim:.2f})")
        if not author_ok and not authors_unknown:
            problems.append("저자 불일치")
        if not year_ok:
            problems.append(detail_year)
        detail = "식별자는 실존하나 " + ", ".join(problems)

    return IndexLookup(
        index_name=index_name,
        outcome=outcome,  # type: ignore[arg-type]
        matched_fields=matched,
        title_similarity=sim,
        detail=detail,
    )
