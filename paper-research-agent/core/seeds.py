"""[0-1] 시드 논문 — 사용자가 아는 논문에서 출발하는 탐색.

## 왜 필요한가

LLM이 만드는 검색어는 질문의 표면 어휘에서 나온다. 사용자가 "경영학 분야
AI agent"라고 쓰면 그 분야 학자들이 실제로 쓰는 말(`firm performance`,
`organizational adoption`, `managerial decision making`)을 알 길이 없다.
프롬프트를 아무리 다듬어도 이건 안 된다 — 분야 지식이 없기 때문이다.

시드 논문은 그 지식을 **사용자가 직접 주입하는 통로**다. "이건 확실히 관련
있다" 싶은 논문 한두 편만 넣으면 세 가지가 생긴다.

1. **실제 학술 어휘** — 제목·초록에서 그 분야가 쓰는 용어를 뽑아 검색어에 쓴다
2. **인용 그래프** — 참고문헌과 피인용 논문을 따라간다. 키워드 검색이
   절대 못 찾는 논문이 여기서 나온다
3. **관련도 기준점** — 시드와 얼마나 닮았는지로 후보를 평가할 수 있다

## 왜 인용 추적이 키워드보다 나은가

키워드 검색은 "그 단어를 쓴 논문"을 찾는다. 인용 추적은 "그 논문이 실제로
학술적으로 연결된 논문"을 찾는다. 같은 개념을 다른 말로 부르는 논문,
제목에 키워드가 없는 논문이 이 경로로만 걸린다.

OpenAlex가 `referenced_works`(참고문헌)와 역방향 조회를 무료로 제공한다.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from core.http_client import IndexUnavailable
from core.indexes import OpenAlexClient, clean_doi
from core.models import PaperCandidate
from core.text_similarity import titles_match

log = logging.getLogger(__name__)

MAX_SEEDS = 5
MAX_REFS_PER_SEED = 25
MAX_CITING_PER_SEED = 25

# 학술 어휘 추출 시 버릴 단어. 분야를 가리지 않고 나와서 변별력이 없다.
_VOCAB_STOP = {
    "the", "a", "an", "of", "and", "or", "for", "in", "on", "to", "with", "by",
    "from", "as", "at", "is", "are", "was", "were", "be", "been", "this", "that",
    "these", "those", "we", "our", "their", "its", "it", "which", "when", "how",
    "using", "used", "use", "based", "study", "studies", "research", "paper",
    "article", "analysis", "approach", "method", "methods", "results", "findings",
    "new", "novel", "toward", "towards", "via", "case", "review", "evidence",
    "effect", "effects", "impact", "role", "between", "among", "more", "most",
    "can", "may", "also", "however", "thus", "than", "such", "both", "not",
}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z][a-z\-]{2,}", (text or "").lower())
            if t not in _VOCAB_STOP]


# ---------------------------------------------------------------------------
# 시드 해석
# ---------------------------------------------------------------------------


def resolve_seed(raw: str, client: OpenAlexClient) -> PaperCandidate | None:
    """사용자가 입력한 한 줄을 실제 논문으로 바꾼다.

    DOI, arXiv ID, OpenAlex URL, 그냥 제목 — 뭘 넣어도 받는다.
    사용자에게 "DOI로 넣으세요"라고 요구하면 대부분 안 쓴다.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    doi = clean_doi(raw)
    if doi:
        try:
            r = client.http.get(f"{OpenAlexClient.BASE}/https://doi.org/{doi}")
            if r.ok and isinstance(r.json_body, dict):
                return client._to_candidate(r.json_body)
        except IndexUnavailable as e:
            log.warning("시드 DOI 조회 실패 (%s): %s", doi, e)
            return None
        except Exception as e:  # noqa: BLE001 — 시드 하나 때문에 전체가 멈추면 안 된다
            log.warning("시드 DOI 처리 중 예외 (%s): %s", doi, e)
            return None
        log.info("시드 DOI를 찾지 못함: %s", doi)
        return None

    # 제목으로 검색 — 가장 비슷한 것을 고르되, 임계값을 넘어야 한다.
    try:
        hits = client.search(raw, limit=5)
    except Exception as e:  # noqa: BLE001
        log.warning("시드 제목 검색 실패 (%s): %s", raw[:50], e)
        return None
    if not hits:
        return None
    for h in hits:
        if titles_match(raw, h.title):
            return h
    # 임계값 미달이면 사용자가 오타를 냈거나 없는 논문이다. 추측하지 않는다.
    log.info("시드 제목과 충분히 일치하는 논문 없음: %s", raw[:60])
    return None


def resolve_seeds(raws: list[str], client: OpenAlexClient) -> tuple[list[PaperCandidate], list[str]]:
    """여러 시드를 해석. (찾은 논문, 못 찾은 입력) 반환."""
    found: list[PaperCandidate] = []
    missing: list[str] = []
    for raw in raws[:MAX_SEEDS]:
        c = resolve_seed(raw, client)
        if c:
            found.append(c)
        elif raw.strip():
            missing.append(raw.strip())
    return found, missing


# ---------------------------------------------------------------------------
# 어휘 추출
# ---------------------------------------------------------------------------


def extract_vocabulary(seeds: list[PaperCandidate], top_n: int = 12) -> list[str]:
    """시드 논문에서 그 분야가 실제로 쓰는 용어를 뽑는다.

    제목에 나온 말에 가중치를 더 준다 — 초록은 길어서 흔한 말이 섞인다.
    두 단어 이상 붙어 나오는 표현(bigram)을 우선한다. `organizational
    adoption`이 `adoption` 하나보다 검색어로 훨씬 쓸모 있다.
    """
    uni: Counter = Counter()
    bi: Counter = Counter()

    for s in seeds:
        for text, weight in ((s.title, 3), (s.abstract or "", 1)):
            toks = _tokens(text)
            for t in toks:
                uni[t] += weight
            for a, b in zip(toks, toks[1:]):
                bi[f"{a} {b}"] += weight

    # 두 개 이상의 시드에 나온 표현을 우선한다 (한 논문만의 특이 용어 배제)
    terms = [p for p, n in bi.most_common(top_n * 2) if n >= 2]
    if len(terms) < top_n:
        terms += [t for t, n in uni.most_common(top_n * 2)
                  if n >= 2 and not any(t in x for x in terms)]
    return terms[:top_n]


def seed_venues(seeds: list[PaperCandidate]) -> list[str]:
    return [s.venue for s in seeds if s.venue]


# ---------------------------------------------------------------------------
# 인용 그래프 추적
# ---------------------------------------------------------------------------


def _openalex_id(cand: PaperCandidate) -> str | None:
    """OpenAlex work ID(W...)를 얻는다. 인용 조회에 필요하다."""
    for src in (cand.url or "", ):
        m = re.search(r"(W\d{6,})", src)
        if m:
            return m.group(1)
    return None


def fetch_references(seed: PaperCandidate, client: OpenAlexClient) -> list[PaperCandidate]:
    """시드가 **인용한** 논문들 (참고문헌). 그 분야의 토대가 되는 문헌."""
    doi = clean_doi(seed.doi)
    if not doi:
        return []
    try:
        r = client.http.get(
            f"{OpenAlexClient.BASE}/https://doi.org/{doi}",
            params={"select": "referenced_works"},
        )
    except IndexUnavailable:
        return []
    if not r.ok or not isinstance(r.json_body, dict):
        return []

    ids = (r.json_body.get("referenced_works") or [])[:MAX_REFS_PER_SEED]
    if not ids:
        return []

    # OpenAlex는 OR 필터로 한 번에 여러 건을 준다. 개별 조회보다 훨씬 빠르다.
    short = [i.rsplit("/", 1)[-1] for i in ids]
    return _fetch_by_ids(short, client)


def fetch_citing(seed: PaperCandidate, client: OpenAlexClient) -> list[PaperCandidate]:
    """시드를 **인용한** 논문들 (피인용). 그 주제의 최신 연구."""
    doi = clean_doi(seed.doi)
    if not doi:
        return []
    try:
        r = client.http.get(
            f"{OpenAlexClient.BASE}/https://doi.org/{doi}", params={"select": "id"}
        )
        if not r.ok or not isinstance(r.json_body, dict):
            return []
        wid = (r.json_body.get("id") or "").rsplit("/", 1)[-1]
        if not wid:
            return []

        r2 = client.http.get(
            OpenAlexClient.BASE,
            params={
                "filter": f"cites:{wid}",
                "per-page": MAX_CITING_PER_SEED,
                "sort": "cited_by_count:desc",
                "select": ("id,doi,display_name,publication_year,authorships,"
                           "primary_location,open_access,best_oa_location,"
                           "abstract_inverted_index"),
            },
        )
    except IndexUnavailable:
        return []
    if not r2.ok or not isinstance(r2.json_body, dict):
        return []
    return [client._to_candidate(w) for w in (r2.json_body.get("results") or [])]


def _fetch_by_ids(work_ids: list[str], client: OpenAlexClient) -> list[PaperCandidate]:
    out: list[PaperCandidate] = []
    # OpenAlex OR 필터는 50개까지 받는다. 안전하게 25개씩.
    for i in range(0, len(work_ids), 25):
        chunk = work_ids[i : i + 25]
        try:
            r = client.http.get(
                OpenAlexClient.BASE,
                params={
                    "filter": f"openalex_id:{'|'.join(chunk)}",
                    "per-page": len(chunk),
                    "select": ("id,doi,display_name,publication_year,authorships,"
                               "primary_location,open_access,best_oa_location,"
                               "abstract_inverted_index"),
                },
            )
        except IndexUnavailable:
            continue
        if r.ok and isinstance(r.json_body, dict):
            out.extend(client._to_candidate(w) for w in (r.json_body.get("results") or []))
    return out


def expand_from_seeds(
    seeds: list[PaperCandidate],
    client: OpenAlexClient,
    use_references: bool = True,
    use_citing: bool = True,
) -> list[PaperCandidate]:
    """시드의 인용 그래프를 한 단계 넓힌다.

    한 단계만 간다. 두 단계 가면 수백 건이 되고 주제에서 멀어진다.
    """
    out: list[PaperCandidate] = []
    for s in seeds:
        # 시드 하나의 인용 조회가 실패해도 나머지는 계속 간다.
        if use_references:
            try:
                refs = fetch_references(s, client)
                log.info("시드 '%s' 참고문헌 %d건", s.title[:40], len(refs))
                out.extend(refs)
            except Exception as e:  # noqa: BLE001
                log.warning("참고문헌 조회 실패 (%s): %s", s.title[:40], e)
        if use_citing:
            try:
                citing = fetch_citing(s, client)
                log.info("시드 '%s' 피인용 %d건", s.title[:40], len(citing))
                out.extend(citing)
            except Exception as e:  # noqa: BLE001
                log.warning("피인용 조회 실패 (%s): %s", s.title[:40], e)
    return out
