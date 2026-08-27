"""문자열 정규화 + 유사도. 검색 클라이언트 3종이 공유한다.

ARS의 `scripts/_text_similarity.py`에서 가져온 설계 판단:
  - 임계값 0.70을 **모듈 상수 한 곳**에 둔다. 클라이언트마다 각자 상수를 들고
    있으면 튜닝할 때 한쪽만 고쳐져 조용히 어긋난다(그쪽 저장소가 v3.9.3에서
    실제로 겪고 뽑아낸 문제).
  - 재시도/백오프 상수도 같은 모듈에 둔다.

여기서는 rapidfuzz 같은 외부 의존 없이 표준 라이브러리 difflib만 쓴다.
논문 제목은 길어야 200자라 성능 문제가 없다.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# --- 공유 상수 (한 곳에서만 정의) --------------------------------------------

TITLE_SIMILARITY_THRESHOLD = 0.70
"""제목이 '같은 논문'이라고 볼 최소 유사도.

0.70은 ARS가 Semantic Scholar 매칭에 쓰는 값. 부제 유무, 하이픈 표기 차이,
전치사 차이 정도는 넘어가되 다른 논문은 걸러내는 지점이다.
"""

BACKOFF_SECONDS = 2.0
MAX_RETRIES = 3

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")

# 제목 앞에 흔히 붙는 노이즈
_LEADING_NOISE_RE = re.compile(
    r"^(?:preprint|working paper|draft|technical report)\s*[:\-–—]\s*",
    flags=re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """비교용 제목 정규화.

    유니코드 정규화 → 선행 노이즈 제거 → 소문자 → 구두점 제거 → 공백 축약.
    """
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    s = _LEADING_NOISE_RE.sub("", s.strip())
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s)
    return s.strip()


def similarity(a: str, b: str) -> float:
    """정규화된 두 제목의 유사도 (0.0 ~ 1.0)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def titles_match(a: str, b: str, threshold: float = TITLE_SIMILARITY_THRESHOLD) -> bool:
    return similarity(a, b) >= threshold


# --- 저자 비교 ---------------------------------------------------------------

_INITIAL_RE = re.compile(r"\b([a-z])\.?\b")


def normalize_author(name: str) -> str:
    """저자명 정규화. 성(last name)만 남긴다.

    'Yann LeCun', 'LeCun, Y.', 'Y. LeCun' → 'lecun'
    이름 표기는 API마다 제각각이라 성만 비교하는 게 실무적으로 안정적이다.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).strip()
    s = _PUNCT_RE.sub(" ", s.lower())
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return ""

    tokens = [t for t in s.split(" ") if len(t) > 1]  # 이니셜 한 글자 제거
    if not tokens:
        return s.replace(" ", "")

    # "lecun, y" 형태는 콤마가 이미 제거됐으므로 첫 토큰이 성.
    # "y lecun" 형태는 마지막 토큰이 성.
    # 구분이 어려우므로 가장 긴 토큰을 성으로 본다 (실무적 휴리스틱).
    return max(tokens, key=len)


def authors_match(
    candidate_authors: list[str],
    reference_authors: list[str],
    min_overlap: int = 1,
) -> bool:
    """저자 리스트가 겹치는지. 최소 min_overlap명 이상 성이 일치하면 True.

    저자 순서/표기/중간이름은 API마다 다르므로 집합 교집합으로만 판정한다.
    """
    if not candidate_authors or not reference_authors:
        return False
    a = {normalize_author(x) for x in candidate_authors}
    b = {normalize_author(x) for x in reference_authors}
    a.discard("")
    b.discard("")
    return len(a & b) >= min_overlap


def author_overlap(candidate_authors: list[str], reference_authors: list[str]) -> int:
    a = {normalize_author(x) for x in candidate_authors} - {""}
    b = {normalize_author(x) for x in reference_authors} - {""}
    return len(a & b)


# --- 인용문 대조용 -----------------------------------------------------------


def normalize_whitespace(s: str) -> str:
    """[7-1] 인용 대조용. PDF 추출 텍스트는 줄바꿈/하이픈이 지저분하다."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00ad", "")          # soft hyphen
    s = re.sub(r"-\s*\n\s*", "", s)      # 줄 끝 하이픈 분철 복원
    s = re.sub(r"[\u2018\u2019]", "'", s)
    s = re.sub(r"[\u201c\u201d]", '"', s)
    s = _WS_RE.sub(" ", s)
    return s.strip()


def normalize_for_quote_match(s: str) -> str:
    """인용 대조는 구두점 차이까지 무시한다 (PDF 추출 노이즈 흡수)."""
    s = normalize_whitespace(s).lower()
    s = _PUNCT_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip()
