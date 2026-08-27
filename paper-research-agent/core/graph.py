"""옵시디언 그래프 뷰를 위한 연결 구조.

## 그래프에 무엇을 엣지로 놓을 것인가

옵시디언 그래프는 `[[위키링크]]`를 엣지로 그린다. 뭘 링크하느냐가 그래프의
쓸모를 결정한다. 여기서는 네 종류를 만든다.

1. **논문 ↔ 논문 (인용)** — 이번에 수집한 논문들 사이의 인용 관계.
   가장 값진 엣지다. 어떤 논문이 이 주제의 토대인지(많이 인용됨),
   어떤 게 최신 확장인지(많이 인용함)가 그래프 모양으로 드러난다.

   **수집한 논문들 사이의 인용만** 그린다. 참고문헌 전체를 링크하면
   존재하지 않는 노드 수백 개가 생겨 그래프가 먼지구름이 된다.

2. **논문 ↔ 키워드** — 키워드 허브 노트를 실제로 만든다. 링크만 걸면
   옵시디언이 '미해결 노드'로 처리해 회색 점으로만 보이고 Dataview 쿼리도
   못 넣는다. 실제 노트로 만들면 그 키워드의 논문 목록이 자동으로 모인다.

3. **논문 ↔ 저널** — 같은 저널에 실린 논문이 묶인다. 분야 경계를 보는 데 쓴다.

4. **논문 ↔ 세션 MOC** — 어느 질문에서 나왔는지.

## 중복 논문

같은 논문이 다른 세션에서 또 나오면 파일명이 `(2)`로 붙어 그래프에 두 개
노드가 생긴다. 그래서 **DOI를 정본 키로 삼아** frontmatter에 넣고, 이미
같은 DOI의 노트가 있으면 새로 만들지 않고 세션 링크만 덧붙인다.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

from core.models import NoteRecord, PaperCandidate

log = logging.getLogger(__name__)

MAX_KEYWORDS_PER_PAPER = 6
MIN_KEYWORD_PAPERS = 2
"""한 논문에만 나온 말은 허브가 되지 않는다. 노드만 늘고 연결은 안 생긴다."""

_STOP = {
    "the", "a", "an", "of", "and", "or", "for", "in", "on", "to", "with", "by",
    "from", "as", "at", "is", "are", "was", "were", "be", "this", "that", "these",
    "we", "our", "their", "its", "it", "which", "when", "how", "using", "used",
    "based", "study", "studies", "research", "paper", "article", "analysis",
    "approach", "method", "methods", "results", "findings", "new", "novel",
    "toward", "towards", "via", "case", "review", "evidence", "effect", "effects",
    "impact", "role", "between", "among", "more", "most", "can", "may", "also",
    "however", "thus", "than", "such", "both", "not", "have", "has", "been",
}


# 문장·구 경계. 불용어를 지운 뒤 남은 토큰을 그냥 이으면 원문에 없던 어구가
# 생긴다("decision making firm performance" → "making firm"). 경계를 남겨 끊는다.
_BREAK = "\x00"


def _phrases(text: str) -> list[str]:
    """인접한 두 단어로 어구를 만든다. 불용어를 사이에 두고 이어붙이지 않는다."""
    toks: list[str] = []
    for t in re.findall(r"[a-z][a-z\-]{2,}|[.,;:()]", (text or "").lower()):
        if t in _STOP or not t[0].isalpha():
            toks.append(_BREAK)      # 여기서 끊는다
        else:
            toks.append(t)
    return [f"{a} {b}" for a, b in zip(toks, toks[1:])
            if a != _BREAK and b != _BREAK]


def slug(s: str) -> str:
    """파일명으로 쓸 수 있는 형태로. 옵시디언이 싫어하는 문자를 뺀다."""
    s = re.sub(r'[<>:"/\\|?*#^\[\]\x00-\x1f]', "", s or "").strip()
    return re.sub(r"\s+", " ", s)[:80]


# ---------------------------------------------------------------------------
# 키워드
# ---------------------------------------------------------------------------


def build_keyword_index(records: list[NoteRecord]) -> dict[str, list[NoteRecord]]:
    """수집한 논문 전체에서 키워드를 뽑고, 논문 2편 이상에 나온 것만 남긴다.

    두 편 이상 조건이 핵심이다. 한 논문에만 나온 말로 허브를 만들면
    노드는 늘고 연결은 안 생겨 그래프가 지저분해진다.
    """
    # 1단계: 각 논문의 후보 어구를 모으고, **몇 편에 나오는지** 전역으로 센다.
    candidates: dict[str, Counter] = {}
    doc_freq: Counter = Counter()
    in_title: set[str] = set()      # 어느 논문이든 제목에 나온 어구

    for r in records:
        c = r.candidate
        title_phrases = _phrases(c.title)
        in_title.update(title_phrases)
        found = Counter(title_phrases * 3 + _phrases(c.abstract or ""))
        candidates[r.note_path] = found
        for phrase in found:
            doc_freq[phrase] += 1

    # 2단계: 공유되는 어구만 남긴 뒤, 논문별로 상위 N개를 고른다.
    #
    # 순서가 중요하다. 논문별로 먼저 자르면 제목 어구가 자리를 다 차지해
    # 여러 논문이 공유하는 어구(그래프에서 실제로 연결을 만드는 것)가 밀린다.
    shared = {p for p, n in doc_freq.items() if n >= MIN_KEYWORD_PAPERS}

    index: dict[str, list[NoteRecord]] = {}
    for r in records:
        mine = [(p, n) for p, n in candidates[r.note_path].items() if p in shared]
        # 제목에 나온 어구를 우대한다. 초록은 문장이 이어져 원문에 없던 어구가
        # 생기지만("...decision making firm performance..." → "making firm"),
        # 제목은 짧고 다듬어져 있어 그런 잡음이 거의 없다.
        mine.sort(
            key=lambda x: (x[0] in in_title, doc_freq[x[0]], x[1]),
            reverse=True,
        )
        for phrase, _ in mine[:MAX_KEYWORDS_PER_PAPER]:
            index.setdefault(phrase, []).append(r)

    return index


def keywords_for(record: NoteRecord, index: dict[str, list[NoteRecord]]) -> list[str]:
    return sorted(kw for kw, recs in index.items() if record in recs)


def write_keyword_notes(index: dict[str, list[NoteRecord]], cfg: dict) -> list[str]:
    """키워드마다 허브 노트를 만든다. 그래프에서 논문들을 묶는 중심이 된다."""
    from core.obsidian_writer import session_root
    kdir = session_root(cfg) / cfg.get("obsidian", {}).get("keywords_dir", "keywords")
    kdir.mkdir(parents=True, exist_ok=True)

    written = []
    for kw, recs in sorted(index.items(), key=lambda x: -len(x[1])):
        name = slug(kw)
        if not name:
            continue
        path = kdir / f"{name}.md"

        lines = [
            "---",
            f'keyword: "{kw}"',
            f"paper_count: {len(recs)}",
            "tags: [keyword]",
            "---",
            "",
            f"# {kw}",
            "",
            f"이 vault에서 **{len(recs)}편**이 이 주제를 다룹니다.",
            "",
            "## 논문",
            "",
        ]
        for r in sorted(recs, key=lambda x: -(x.candidate.year or 0)):
            y = r.candidate.year or "n.d."
            depth = {"full_text": "전문", "abstract_only": "초록", }.get(
                r.summary.depth if r.summary else "", "요약 없음"
            )
            lines.append(f"- [[{Path(r.note_path).stem}]] ({y}) — {depth}")

        lines += [
            "",
            "> [!tip] 그래프에서 보기",
            "> 이 노트를 그래프 뷰에서 열면 이 키워드를 공유하는 논문들이 함께 보입니다.",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        written.append(str(path))

    log.info("키워드 허브 노트 %d개 생성", len(written))
    return written


# ---------------------------------------------------------------------------
# 인용 엣지
# ---------------------------------------------------------------------------


def build_citation_edges(
    records: list[NoteRecord], client
) -> dict[str, dict[str, list[str]]]:
    """수집한 논문들 **사이의** 인용 관계를 찾는다.

    Returns:
        {note_stem: {"cites": [note_stem...], "cited_by": [note_stem...]}}

    참고문헌 전체가 아니라 이번에 모은 것들 사이만 본다. 전체를 링크하면
    존재하지 않는 노드가 수백 개 생겨 그래프가 못 쓰게 된다.
    """
    from core.indexes import clean_doi
    from core.seeds import fetch_references

    doi_to_stem: dict[str, str] = {}
    for r in records:
        d = clean_doi(r.candidate.doi)
        if d:
            doi_to_stem[d] = Path(r.note_path).stem

    edges: dict[str, dict[str, list[str]]] = {
        Path(r.note_path).stem: {"cites": [], "cited_by": []} for r in records
    }
    if len(doi_to_stem) < 2:
        return edges   # 비교할 대상이 없다

    for r in records:
        stem = Path(r.note_path).stem
        try:
            refs = fetch_references(r.candidate, client)
        except Exception as e:  # noqa: BLE001 — 그래프는 부가 기능이다
            log.warning("인용 관계 조회 실패 (%s): %s", stem, e)
            continue

        for ref in refs:
            d = clean_doi(ref.doi)
            target = doi_to_stem.get(d) if d else None
            if target and target != stem:
                if target not in edges[stem]["cites"]:
                    edges[stem]["cites"].append(target)
                if stem not in edges[target]["cited_by"]:
                    edges[target]["cited_by"].append(stem)

    total = sum(len(v["cites"]) for v in edges.values())
    log.info("논문 간 인용 엣지 %d개 발견", total)
    return edges


# ---------------------------------------------------------------------------
# 중복 논문
# ---------------------------------------------------------------------------


def find_existing_note(candidate: PaperCandidate, cfg: dict) -> Path | None:
    """같은 DOI의 노트가 이미 vault에 있는지 찾는다.

    파일명은 제목에서 만들어져 표기가 조금만 달라도 다른 파일이 된다.
    DOI를 정본 키로 삼아야 그래프에 같은 논문이 두 노드로 갈라지지 않는다.
    """
    from core.indexes import clean_doi

    doi = clean_doi(candidate.doi)
    if not doi:
        return None

    from core.obsidian_writer import session_root
    pdir = session_root(cfg) / cfg.get("obsidian", {}).get("papers_dir", "papers")
    if not pdir.is_dir():
        return None

    needle = f'doi: "{doi}"'
    for f in pdir.glob("*.md"):
        try:
            head = f.read_text(encoding="utf-8")[:1200]
        except OSError:
            continue
        if needle in head:
            return f
    return None
