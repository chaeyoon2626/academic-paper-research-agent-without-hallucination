"""[6] 텍스트 추출.

페이지 단위로 뽑는다. 페이지 번호가 필요한 이유는 [7]에서 인용에
`(p.12: "...")` 형식으로 페이지를 강제하고, [7-1]에서 그 페이지 텍스트와
대조해야 하기 때문이다.

**스캔본 탐지가 중요하다.** 이미지 스캔 PDF는 추출 텍스트가 거의 비는데,
그 빈 텍스트를 LLM에 넘기면 모델이 제목만 보고 내용을 지어낸다.
기획서 원칙 2가 막으려는 바로 그 시나리오다. 그래서 예외를 던진다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.models import ParsedPage, ParsedPaper

log = logging.getLogger(__name__)

MIN_CHARS_PER_PAGE = 120
MIN_TOTAL_CHARS = 1500


class ScannedPdfError(Exception):
    """추출 텍스트가 비정상적으로 적다. 스캔본이거나 추출 실패."""


class PdfParseError(Exception):
    """PDF를 열 수 없다 (손상/암호화)."""


def _extract_with_pdfplumber(path: str) -> list[ParsedPage]:
    import pdfplumber

    pages: list[ParsedPage] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as e:  # noqa: BLE001 — 페이지 하나가 깨져도 계속
                log.debug("p.%d 추출 실패: %s", i, e)
                text = ""
            pages.append(ParsedPage(page_no=i, text=text.strip()))
    return pages


def _extract_with_pypdf(path: str) -> list[ParsedPage]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            log.debug("p.%d 추출 실패: %s", i, e)
            text = ""
        pages.append(ParsedPage(page_no=i, text=text.strip()))
    return pages


def parse_pdf(path: str) -> ParsedPaper:
    """페이지 단위 텍스트 추출.

    Raises:
        PdfParseError:   파일을 열 수 없음
        ScannedPdfError: 추출량이 비정상적으로 적음
    """
    p = Path(path)
    if not p.exists():
        raise PdfParseError(f"파일이 없음: {path}")

    pages: list[ParsedPage] = []
    errors: list[str] = []

    for extractor in (_extract_with_pdfplumber, _extract_with_pypdf):
        try:
            pages = extractor(str(p))
            if pages and sum(len(x.text) for x in pages) >= MIN_TOTAL_CHARS:
                break
        except Exception as e:  # noqa: BLE001 — 다음 추출기로 폴백
            errors.append(f"{extractor.__name__}: {e}")
            continue

    if not pages:
        raise PdfParseError(f"텍스트 추출 실패: {'; '.join(errors) or '알 수 없는 오류'}")

    paper = ParsedPaper(path=str(p), pages=pages)

    if paper.char_count < MIN_TOTAL_CHARS:
        raise ScannedPdfError(
            f"추출 텍스트가 {paper.char_count}자뿐 (기준 {MIN_TOTAL_CHARS}자). "
            f"스캔본이거나 추출 실패로 판단 — 요약하지 않음"
        )

    non_empty = sum(1 for x in pages if len(x.text) >= MIN_CHARS_PER_PAGE)
    if non_empty < max(1, len(pages) // 4):
        raise ScannedPdfError(
            f"{len(pages)}페이지 중 {non_empty}페이지만 텍스트 보유. 스캔본으로 판단"
        )

    log.info("추출 완료: %d페이지 %d자", len(pages), paper.char_count)
    return paper


def build_paged_text(paper: ParsedPaper, max_chars: int | None = None) -> str:
    """LLM에 넘길 형태로 조립. 페이지 마커를 넣어 모델이 페이지를 인용할 수 있게 한다.

    max_chars를 넘으면 뒷부분을 자른다. 논문은 앞쪽(초록·서론·방법)에
    핵심이 몰려 있고, 뒤쪽은 참고문헌이라 자르는 게 손해가 적다.
    """
    parts = []
    total = 0
    for page in paper.pages:
        if not page.text:
            continue
        block = f"\n===== [p.{page.page_no}] =====\n{page.text}"
        if max_chars is not None and total + len(block) > max_chars:
            parts.append(f"\n\n[...이후 {len(paper.pages) - page.page_no + 1}페이지 생략...]")
            break
        parts.append(block)
        total += len(block)
    return "".join(parts).strip()
