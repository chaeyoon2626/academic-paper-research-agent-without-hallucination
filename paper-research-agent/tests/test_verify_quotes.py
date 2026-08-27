"""[7-1] 인용 재검증 테스트.

핵심 질문 두 가지:
  1. 지어낸 인용을 실제로 잡는가?
  2. **진짜 인용을 실수로 떨어뜨리지 않는가?** (이게 더 중요하다.
     오탐이 많으면 사용자가 ⚠ 표시를 무시하기 시작하고, 그러면 기능이 죽는다.)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core.models import ParsedPage, ParsedPaper, QuoteClaim, Summary  # noqa: E402
from core.summarize import parse_summary_markdown  # noqa: E402
from core.verify_quotes import verify_quotes  # noqa: E402

PAPER = ParsedPaper(
    path="fake.pdf",
    pages=[
        ParsedPage(1, "We propose a new architecture based solely on attention mechanisms, "
                      "dispensing with recurrence and convolutions entirely."),
        ParsedPage(2, "Our model achieves 28.4 BLEU on the WMT 2014 English-to-German "
                      "translation task, improving over the existing best results."),
        ParsedPage(3, "We are excited about the future of attention-based models and plan "
                      "to extend them to other modalities."),
    ],
)


def _summary(*quotes: QuoteClaim) -> Summary:
    return Summary(depth="full_text", quotes=list(quotes))


# --- 진짜 인용은 통과해야 한다 --------------------------------------------------


def test_exact_quote_passes():
    s = _summary(QuoteClaim(page=1, quote="dispensing with recurrence and convolutions entirely"))
    verify_quotes(s, paper=PAPER)
    assert s.quotes[0].verified


def test_quote_with_pdf_noise_still_passes():
    """PDF 추출은 줄바꿈과 하이픈 분철을 남긴다. 그것 때문에 떨어지면 안 된다."""
    s = _summary(QuoteClaim(page=2, quote="Our model achieves 28.4 BLEU on the WMT 2014\nEnglish-to-German"))
    verify_quotes(s, paper=PAPER)
    assert s.quotes[0].verified


def test_quote_with_different_quotemarks_passes():
    s = _summary(QuoteClaim(page=1, quote="We propose a new architecture based solely on attention mechanisms,"))
    verify_quotes(s, paper=PAPER)
    assert s.quotes[0].verified


# --- 지어낸 인용은 걸려야 한다 --------------------------------------------------


def test_fabricated_quote_fails():
    s = _summary(QuoteClaim(page=2, quote="Our model achieves 99.9 accuracy on every benchmark ever created"))
    verify_quotes(s, paper=PAPER)
    assert not s.quotes[0].verified


def test_plausible_but_absent_quote_fails():
    """논문에 있을 법하지만 실제로는 없는 문장. 이게 가장 위험한 유형이다."""
    s = _summary(QuoteClaim(page=3, quote="We conducted extensive ablation studies across twelve datasets"))
    verify_quotes(s, paper=PAPER)
    assert not s.quotes[0].verified


# --- 페이지 번호가 틀린 경우 ----------------------------------------------------


def test_wrong_page_but_real_quote_passes_with_flag():
    """인용은 진짜인데 페이지만 틀린 경우 — 통과시키되 표시한다.
    지어낸 것과는 다른 종류의 오류이므로 다르게 다뤄야 한다."""
    s = _summary(QuoteClaim(page=1, quote="Our model achieves 28.4 BLEU on the WMT 2014"))
    verify_quotes(s, paper=PAPER)
    assert s.quotes[0].verified
    assert "페이지 번호 불일치" in s.quotes[0].claim_text


# --- 너무 짧은 인용 ------------------------------------------------------------


def test_too_short_quote_is_not_auto_passed():
    """짧은 인용은 우연히 일치한다. 통과시키면 검증이 무의미해진다."""
    s = _summary(QuoteClaim(page=1, quote="the"))
    verify_quotes(s, paper=PAPER)
    assert not s.quotes[0].verified


# --- 마크다운 파싱 -------------------------------------------------------------


def test_parse_summary_markdown_extracts_quotes():
    md = '''## 배경
어텐션만으로 구성된 구조를 제안한다. (p.1: "dispensing with recurrence and convolutions entirely")

## 방법
[근거 없음]

## 핵심 결과
WMT 2014에서 28.4 BLEU를 달성했다. (p.2: "Our model achieves 28.4 BLEU on the WMT 2014")

## 한계
[근거 없음]
'''
    s = parse_summary_markdown(md)
    assert len(s.quotes) == 2
    assert s.quotes[0].page == 1
    assert s.quotes[1].page == 2
    assert s.depth == "full_text"
    assert "어텐션만으로" in s.background
    assert s.method.strip() == "[근거 없음]"

    verify_quotes(s, paper=PAPER)
    assert s.verified_quote_count == 2


def test_end_to_end_catches_hallucinated_summary():
    """LLM이 그럴듯하지만 원문에 없는 내용을 쓴 경우 전체 흐름에서 잡히는가."""
    md = '''## 핵심 결과
모델이 모든 벤치마크에서 최고 성능을 냈다. (p.2: "state of the art on all seventeen benchmarks tested")
'''
    s = parse_summary_markdown(md)
    verify_quotes(s, paper=PAPER)
    assert s.failed_quote_count == 1
    assert s.verified_quote_count == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
