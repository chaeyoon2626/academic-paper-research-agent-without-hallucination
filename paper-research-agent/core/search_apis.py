"""[2] 논문 후보 검색.

기획서 §6의 시그니처를 그대로 유지한다. 실제 HTTP는 `core/indexes.py`가 담당.
한쪽 API가 실패해도 나머지 결과는 반환한다(개별 try/except).
"""

from __future__ import annotations

import logging

from core.indexes import ArxivClient, OpenAlexClient
from core.models import PaperCandidate

log = logging.getLogger(__name__)


def search_openalex(
    query: str, limit: int, cfg: dict, client: OpenAlexClient | None = None
) -> list[PaperCandidate]:
    c = client or OpenAlexClient(mailto=cfg.get("contact_email"))
    try:
        return c.search(query, limit)
    except Exception as e:  # noqa: BLE001 — 한 소스 실패가 전체를 죽이면 안 된다
        log.warning("OpenAlex 검색 중 예외: %s", e)
        return []


def search_arxiv(
    query: str, limit: int, client: ArxivClient | None = None
) -> list[PaperCandidate]:
    c = client or ArxivClient()
    try:
        return c.search(query, limit)
    except Exception as e:  # noqa: BLE001
        log.warning("arXiv 검색 중 예외: %s", e)
        return []


def dedupe(candidates: list[PaperCandidate]) -> list[PaperCandidate]:
    """identity_key 기준 중복 제거. 먼저 온 쪽을 남기되 필드는 병합한다.

    OpenAlex가 메타데이터는 풍부한데 PDF 링크가 없고, arXiv가 그 반대인
    경우가 흔하다. 그냥 버리면 전문 확보율이 떨어진다.
    """
    seen: dict[str, PaperCandidate] = {}
    for c in candidates:
        key = c.identity_key()
        if key not in seen:
            seen[key] = c
            continue
        kept = seen[key]
        for fld in ("doi", "arxiv_id", "venue", "abstract", "url", "oa_pdf_url", "year"):
            if not getattr(kept, fld) and getattr(c, fld):
                setattr(kept, fld, getattr(c, fld))
        if len(c.authors) > len(kept.authors):
            kept.authors = c.authors
    return list(seen.values())


def search_all(
    query: str,
    cfg: dict,
    openalex: OpenAlexClient | None = None,
    arxiv: ArxivClient | None = None,
) -> list[PaperCandidate]:
    """OpenAlex + arXiv를 합쳐 중복 제거한 후보 목록."""
    limit = int(cfg.get("search", {}).get("per_source_limit", 10))
    results: list[PaperCandidate] = []
    results.extend(search_openalex(query, limit, cfg, client=openalex))

    if cfg.get("search", {}).get("use_arxiv", True):
        results.extend(search_arxiv(query, limit, client=arxiv))

    merged = dedupe(results)
    log.info("쿼리 '%s': 후보 %d건 (중복 제거 전 %d건)", query, len(merged), len(results))
    return merged
