"""데이터 구조 정의. 함수 없음, 데이터클래스만.

기획서 §6 `core/models.py`에 대응.
ARS 참조 반영 사항:
  - VerificationStatus에 `uncertain`을 "API가 대답을 못 준 경우"의 흡수 상태로 명시.
    (ARS의 lookup_verified = {true, false, unresolvable} 3-상태와 같은 의도)
  - IndexLookup: 인덱스(OpenAlex/Crossref/arXiv)별 조회 결과를 개별 기록.
    한 인덱스가 죽어도 다른 인덱스 결과로 판정할 수 있게 하기 위함.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

# ---------------------------------------------------------------------------
# 리터럴 타입
# ---------------------------------------------------------------------------

VerificationStatus = Literal["verified", "uncertain", "not_found", "irrelevant"]
"""검증 판정.

verified   : 식별자 + 저자 + 연도가 권위 인덱스에서 함께 일치.
uncertain  : 판정 불가. **API 장애/미색인/식별자 부재가 전부 여기로 들어온다.**
             중요: "확인 못 함"과 "가짜"는 다르다. 인덱스가 죽었을 때
             not_found로 떨어뜨리면 멀쩡한 논문이 가짜로 찍힌다.
not_found  : 식별자로 조회했는데 인덱스가 명시적으로 "없다"고 답한 경우에만.
irrelevant : 존재는 확인됐지만([3]) 질문과 무관하다고 LLM이 판단함([3-1]).
"""

OAStatus = Literal["free", "paid", "unknown"]

FullTextFailure = Literal[
    "none",              # 실패 없음 (전문 확보 성공)
    "no_oa_link",        # 무료 사본 자체를 못 찾음 — 진짜 페이월이거나 미색인
    "download_failed",   # 링크는 있는데 네트워크/HTTP 실패 (403 봇 차단 등)
    "not_a_pdf",         # 받아왔는데 PDF가 아님 — 대개 랜딩 페이지나 로그인 화면
    "scanned_or_empty",  # PDF는 맞는데 텍스트가 없음 (스캔본)
    "parse_failed",      # PDF가 손상되었거나 암호화됨
    "summarize_failed",  # 전문은 확보했는데 LLM 요약이 실패
    "skipped",           # 사용자가 전문 확보를 꺼둠 (실패가 아니다)
]
"""전문을 못 구한 이유. **원인마다 대응이 다르기 때문에 구분한다.**

no_oa_link 가 대부분이면 → 소스를 늘려야 한다 (CORE 등)
not_a_pdf 가 대부분이면 → 랜딩 페이지에서 PDF를 찾아내야 한다
download_failed 가 대부분이면 → 봇 차단 회피(User-Agent, Referer)가 필요하다

이걸 구분하지 않으면 "전문이 안 구해진다"는 하나의 증상만 보이고,
소스를 늘려야 할지 다운로드를 고쳐야 할지 알 수 없다.
"""
SummaryDepth = Literal["full_text", "abstract_only", "no_summary"]
"""요약의 근거 수준. **절대 뭉뚱그리면 안 된다.**

full_text     : 전문을 읽고 요약. 페이지 단위 인용 대조까지 통과.
abstract_only : 초록만 보고 정리. 방법 상세가 대개 빠져 있다.
                논문 선별용으로는 쓸 만하지만, 인용하거나 방법을 논할 근거로는
                쓸 수 없다. 노트와 UI에서 항상 눈에 띄게 구분한다.
no_summary    : 요약하지 않음.
"""
VenueTier = Literal["top", "normal", "unknown"]
LookupOutcome = Literal["match", "mismatch", "absent", "unavailable"]
"""개별 인덱스 조회 결과.

match       : 조회됨 + 메타데이터 일치
mismatch    : 조회됨 + 메타데이터 불일치 (저자/연도가 다름 → 위조 신호)
absent      : 인덱스가 "그런 식별자 없음"이라고 명시적으로 응답
unavailable : 타임아웃/5xx/레이트리밋/식별자 없어서 조회 자체 불가
"""


# ---------------------------------------------------------------------------
# 후보 논문
# ---------------------------------------------------------------------------


@dataclass
class PaperCandidate:
    """[2] 검색 단계에서 수집된 논문 후보 하나."""

    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    venue: str | None = None
    abstract: str | None = None
    url: str | None = None
    oa_pdf_url: str | None = None
    source_api: str = "unknown"

    def identity_key(self) -> str:
        """중복 제거용 키. DOI > arXiv ID > 정규화 제목 순."""
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id.lower()}"
        from core.text_similarity import normalize_title

        return f"title:{normalize_title(self.title)}"

    def has_identifier(self) -> bool:
        return bool(self.doi or self.arxiv_id)


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------


@dataclass
class IndexLookup:
    """단일 인덱스에 대한 조회 결과 1건."""

    index_name: str  # "openalex" | "crossref" | "arxiv"
    outcome: LookupOutcome
    matched_fields: list[str] = field(default_factory=list)
    title_similarity: float | None = None
    detail: str = ""

    @property
    def is_positive(self) -> bool:
        return self.outcome == "match"

    @property
    def is_negative(self) -> bool:
        """'명시적으로 없다'고 확인된 경우만 True. unavailable은 False."""
        return self.outcome in ("absent", "mismatch")


@dataclass
class VerificationResult:
    """[3] 존재 검증 결과."""

    candidate: PaperCandidate
    status: VerificationStatus
    matched_fields: list[str] = field(default_factory=list)
    title_similarity: float = 0.0
    lookups: list[IndexLookup] = field(default_factory=list)
    reason: str = ""

    @property
    def is_verified(self) -> bool:
        return self.status == "verified"

    def summary_line(self) -> str:
        parts = [f"{lk.index_name}={lk.outcome}" for lk in self.lookups]
        return f"[{self.status}] {' '.join(parts)} :: {self.reason}"


# ---------------------------------------------------------------------------
# 오픈 액세스 / 요약
# ---------------------------------------------------------------------------


@dataclass
class OAResult:
    """[5] 오픈 액세스 확인 결과."""

    status: OAStatus = "unknown"
    pdf_url: str | None = None
    source: str = ""

    # CORE 등이 링크 대신 텍스트를 직접 준 경우. 이게 있으면 PDF 단계를 건너뛴다.
    full_text: str | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.full_text and len(self.full_text.strip()) >= 1500)

    @property
    def is_free(self) -> bool:
        return self.status == "free" and bool(self.pdf_url)


@dataclass
class ParsedPage:
    page_no: int
    text: str


@dataclass
class ParsedPaper:
    """[6] 텍스트 추출 결과."""

    path: str
    pages: list[ParsedPage] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for p in self.pages)


@dataclass
class QuoteClaim:
    """[7] 요약이 주장하는 인용문 1건. [7-1]에서 verified가 채워진다."""

    page: int | None
    quote: str
    claim_text: str = ""
    verified: bool = False
    match_ratio: float = 0.0


@dataclass
class Summary:
    """[7] 구조화된 요약."""

    background: str = ""
    method: str = ""
    key_results_md: str = ""
    limitations: str = ""
    related_work: str = ""
    conclusion: str = ""
    # [초록 기반] 본문에서 확인해야 할 것들. 무엇을 모르는지 명시하는 칸이다.
    open_questions: str = ""
    quotes: list[QuoteClaim] = field(default_factory=list)
    depth: SummaryDepth = "no_summary"
    raw_markdown: str = ""

    @property
    def verified_quote_count(self) -> int:
        return sum(1 for q in self.quotes if q.verified)

    @property
    def failed_quote_count(self) -> int:
        return sum(1 for q in self.quotes if not q.verified)

    @property
    def method_is_missing(self) -> bool:
        """방법 항목이 비었는지. 초록 기반 요약에서 특히 중요하다 —
        방법을 모르는 채로 논문을 고르면 엉뚱한 걸 고른다."""
        m = (self.method or "").strip()
        return not m or "초록에 없음" in m or "근거 없음" in m


# ---------------------------------------------------------------------------
# 출력 레코드 / 세션 로그
# ---------------------------------------------------------------------------


@dataclass
class NoteRecord:
    """[8] 저장된 노트 1건에 대한 기록."""

    candidate: PaperCandidate
    verification: VerificationResult
    oa: OAResult
    summary: Summary | None
    note_path: str
    venue_tier: VenueTier = "unknown"
    pdf_local_path: str | None = None
    # [5]~[7] 중 어디서 막혔는지. 세션 노트의 진단 표를 만드는 근거.
    fulltext_failure: FullTextFailure = "none"
    fulltext_detail: str = ""


@dataclass
class SessionLogEntry:
    query: str
    candidates_found: int = 0
    verified_count: int = 0
    uncertain_count: int = 0
    not_found_count: int = 0
    model_used: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
