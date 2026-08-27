"""[7-1] 인용문 재검증. LLM을 쓰지 않는다. 문자열 대조만 한다.

**실패한 인용은 지우지 않는다.** 노트에 "⚠ 원문 대조 실패"로 표시하고 남긴다.
조용히 지우면 사용자는 요약이 100% 검증된 것처럼 착각한다 — 원칙 3.

ARS의 3계층 인용 앵커(quote / page / section)에서 quote+page 두 계층을 가져왔다.
그쪽이 인용 앵커를 25단어로 제한하는 이유도 여기 적용된다: 인용이 길수록
PDF 추출 노이즈(줄바꿈, 하이픈 분철, 리가처)로 대조가 실패할 확률이 올라간다.
길이 제한이 없으면 **진짜 인용도 대조 실패로 찍힌다.**
"""

from __future__ import annotations

import logging
from difflib import SequenceMatcher

from core.models import ParsedPaper, Summary
from core.text_similarity import normalize_for_quote_match, normalize_whitespace

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 0.90
"""퍼지 대조 임계값. 정확 일치보다 낮게 잡는 이유는 PDF 추출 노이즈 때문이다.
0.90이면 리가처(fi/fl)나 공백 차이는 넘어가되 다른 문장은 걸러낸다."""

MAX_QUOTE_WORDS = 25


def _best_window_ratio(needle: str, haystack: str) -> float:
    """haystack 안에서 needle과 가장 비슷한 구간의 유사도.

    needle 길이의 슬라이딩 윈도우로 훑는다. 전체 문서를 한 번에 비교하면
    긴 haystack 때문에 ratio가 항상 낮게 나온다.
    """
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0

    n = len(needle)
    if n >= len(haystack):
        return SequenceMatcher(None, needle, haystack).ratio()

    best = 0.0
    step = max(1, n // 4)
    for i in range(0, len(haystack) - n + 1, step):
        window = haystack[i : i + n + n // 5]
        r = SequenceMatcher(None, needle, window).ratio()
        if r > best:
            best = r
            if best >= 0.99:
                break
    return best


def verify_quotes(summary: Summary, paper: ParsedPaper | None = None,
                  source_text: str | None = None) -> Summary:
    """각 QuoteClaim이 원문에 실제로 있는지 확인하고 verified를 채운다.

    paper가 주어지면 페이지 단위로 대조한다(더 엄격 — 페이지 번호까지 검증).
    source_text만 주어지면 전체 텍스트에서 대조한다.
    """
    if paper is None and source_text is None:
        raise ValueError("paper 또는 source_text 중 하나는 필요합니다")

    full_norm = (
        normalize_for_quote_match(paper.full_text) if paper
        else normalize_for_quote_match(source_text or "")
    )
    page_norm: dict[int, str] = {}
    if paper:
        page_norm = {p.page_no: normalize_for_quote_match(p.text) for p in paper.pages}

    for q in summary.quotes:
        raw = normalize_whitespace(q.quote)
        words = raw.split()

        # 너무 긴 인용은 앞 25단어로 잘라서 대조 (위 모듈 주석 참조)
        probe = " ".join(words[:MAX_QUOTE_WORDS]) if len(words) > MAX_QUOTE_WORDS else raw
        needle = normalize_for_quote_match(probe)

        if len(needle) < 15:
            # 너무 짧으면 우연 일치가 나온다. 검증 불가로 둔다.
            q.verified = False
            q.match_ratio = 0.0
            continue

        # 1) 모델이 말한 그 페이지에서 먼저 찾는다
        ratio = 0.0
        if q.page is not None and q.page in page_norm:
            ratio = _best_window_ratio(needle, page_norm[q.page])

        # 2) 그 페이지에 없으면 전체에서 찾는다.
        #    찾아지면 "인용은 진짜인데 페이지가 틀림" → 통과시키되 기록.
        if ratio < FUZZY_THRESHOLD:
            whole = _best_window_ratio(needle, full_norm)
            if whole > ratio:
                if whole >= FUZZY_THRESHOLD and q.page is not None and page_norm:
                    q.claim_text = (q.claim_text + " [페이지 번호 불일치]").strip()
                ratio = whole

        q.match_ratio = round(ratio, 3)
        q.verified = ratio >= FUZZY_THRESHOLD

    ok = summary.verified_quote_count
    total = len(summary.quotes)
    if total:
        log.info("인용 검증: %d/%d 통과", ok, total)
        if ok < total:
            log.warning("원문 대조 실패 인용 %d건 — 노트에 표시됨", total - ok)
    return summary
