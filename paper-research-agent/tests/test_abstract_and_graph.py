"""[7-A] 초록 기반 정리와 [8-1] 그래프 구조 테스트.

핵심 질문:
  1. 초록에 없는 방법을 지어내면 잡히는가? (가장 위험한 실패)
  2. 초록 기반이 전문 기반과 **절대 섞이지 않는가**?
  3. 그래프 링크가 실제로 만들어지고, 무관한 논문은 고립되는가?
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core.graph import _phrases, build_keyword_index, keywords_for, write_keyword_notes  # noqa: E402
from core.models import (  # noqa: E402
    NoteRecord,
    OAResult,
    PaperCandidate,
    Summary,
    VerificationResult,
)
from core.obsidian_writer import enrich_note, write_note  # noqa: E402
from core.summarize import EmptyTextError, parse_abstract_markdown, summarize_abstract  # noqa: E402
from core.verify_quotes import verify_quotes  # noqa: E402

ABSTRACT = (
    "This study examines how firms adopt AI agents in managerial decision making. "
    "Drawing on organizational adoption theory, we find that firm performance "
    "improves when agents augment rather than replace managers. We conclude that "
    "governance structures determine adoption outcomes."
)


# ---------------------------------------------------------------------------
# 초록 기반 정리: 날조 탐지
# ---------------------------------------------------------------------------


def test_fabricated_method_is_caught():
    """★ 초록에 없는 방법을 지어내면 인용 대조에서 걸려야 한다.

    이게 이 기능의 가장 큰 위험이다. 초록에는 표본·측정·분석 기법이 거의
    없는데, "방법을 요약하라"고 하면 모델이 아는 대로 채운다.
    """
    md = '''## 방법
280개 기업 설문과 위계적 회귀분석. ("we surveyed 280 firms and conducted hierarchical regression")
'''
    s = verify_quotes(parse_abstract_markdown(md), source_text=ABSTRACT)
    assert s.failed_quote_count == 1
    assert not s.quotes[0].verified


def test_real_quote_from_abstract_passes():
    md = '''## 핵심 결과
성과가 개선된다. ("firm performance improves when agents augment rather than replace managers")
'''
    s = verify_quotes(parse_abstract_markdown(md), source_text=ABSTRACT)
    assert s.verified_quote_count == 1


def test_method_missing_is_detected():
    """방법이 비었다는 걸 코드가 알아야 노트와 UI에 경고를 띄운다."""
    s = parse_abstract_markdown("## 방법\n[초록에 없음]\n")
    assert s.method_is_missing

    s2 = parse_abstract_markdown("## 방법\n설문조사를 실시했다.\n")
    assert not s2.method_is_missing


def test_depth_is_never_full_text():
    """★ 초록 기반이 전문 기반으로 둔갑하면 안 된다."""
    s = parse_abstract_markdown("## 배경\n내용\n")
    assert s.depth == "abstract_only"


def test_short_abstract_is_refused():
    """짧은 초록을 억지로 늘리면 모델이 지어낸다."""
    with pytest.raises(EmptyTextError):
        summarize_abstract("짧음", {})
    with pytest.raises(EmptyTextError):
        summarize_abstract("", {})


def test_open_questions_parsed():
    md = "## 전문 확인이 필요한 부분\n- 표본 크기\n- 측정 도구\n"
    s = parse_abstract_markdown(md)
    assert "표본 크기" in s.open_questions


# ---------------------------------------------------------------------------
# 노트: 신뢰 수준이 눈에 보이는가
# ---------------------------------------------------------------------------


def _note(depth, tmp_path, method="[초록에 없음]"):
    c = PaperCandidate(title="Some Paper", authors=["Kim"], year=2024,
                       doi="10.1016/j.a.2024.1", venue="J. Bus. Res.")
    v = VerificationResult(candidate=c, status="verified", reason="확인됨")
    s = Summary(depth=depth, background="배경", method=method, conclusion="결론")
    path = write_note(c, v, OAResult(), s, None,
                      {"obsidian": {"vault_path": str(tmp_path)}})
    return Path(path).read_text(encoding="utf-8")


def test_abstract_note_carries_visible_warning(tmp_path):
    """★ 노트를 읽는 사람이 전문 요약으로 착각하면 안 된다."""
    text = _note("abstract_only", tmp_path)
    assert "summary_depth: abstract_only" in text
    assert "전문 요약이 아닙니다" in text
    assert "방법은 초록에 나오지 않아" in text


def test_full_text_note_has_no_abstract_warning(tmp_path):
    text = _note("full_text", tmp_path, method="설문조사")
    assert "summary_depth: full_text" in text
    assert "전문 요약이 아닙니다" not in text


def test_abstract_note_tag_is_distinct(tmp_path):
    """vault에서 `summary/abstract_only`로 걸러낼 수 있어야 한다."""
    assert "summary/abstract_only" in _note("abstract_only", tmp_path)


# ---------------------------------------------------------------------------
# 그래프 구조
# ---------------------------------------------------------------------------


def _rec(title, abstract, key):
    c = PaperCandidate(title=title, abstract=abstract, year=2024, doi=f"10.1016/j.{key}.2024.1")
    return NoteRecord(
        candidate=c,
        verification=VerificationResult(candidate=c, status="verified"),
        oa=OAResult(), summary=Summary(depth="full_text"),
        note_path=f"/v/papers/{key}.md",
    )


BUSINESS = [
    _rec("Organizational adoption of AI agents",
         "organizational adoption managerial decision making firm performance", "a"),
    _rec("Generative AI and firm performance",
         "organizational adoption generative AI firm performance", "b"),
    _rec("Managerial decision making with agents",
         "managerial decision making firm performance agents", "c"),
]
MEDICAL = _rec("Deep learning for retinal image segmentation",
               "retinal image segmentation medical imaging convolutional networks", "d")


def test_shared_keywords_become_hubs():
    index = build_keyword_index(BUSINESS)
    assert "firm performance" in index
    assert len(index["firm performance"]) == 3


def test_unrelated_paper_stays_isolated():
    """★ 무관한 논문이 그래프에서 고립돼야 군집이 의미를 갖는다."""
    index = build_keyword_index(BUSINESS + [MEDICAL])
    linked = {r.note_path for recs in index.values() for r in recs}
    assert MEDICAL.note_path not in linked


def test_single_paper_phrase_is_not_a_hub():
    """한 논문에만 나온 말로 허브를 만들면 노드만 늘고 연결은 안 생긴다."""
    index = build_keyword_index(BUSINESS)
    for kw, recs in index.items():
        assert len(recs) >= 2, f"{kw}가 한 편에만 연결됨"


def test_phrases_do_not_cross_stopword_boundary():
    """불용어를 사이에 두고 이어붙이면 원문에 없던 어구가 생긴다."""
    got = _phrases("organizational adoption of AI agents")
    assert "adoption agents" in got or "organizational adoption" in got
    assert "adoption of" not in got


def test_keyword_notes_are_written(tmp_path):
    index = build_keyword_index(BUSINESS)
    paths = write_keyword_notes(index, {"obsidian": {"vault_path": str(tmp_path)}})
    assert paths
    text = Path(paths[0]).read_text(encoding="utf-8")
    assert "tags: [keyword]" in text
    assert "[[" in text, "위키링크가 없으면 그래프 엣지가 안 생긴다"


def test_enrich_adds_wikilinks(tmp_path):
    c = PaperCandidate(title="Paper A", authors=["Kim"], year=2024,
                       doi="10.1016/j.a.2024.1", venue="J. Bus. Res.")
    v = VerificationResult(candidate=c, status="verified")
    path = write_note(c, v, OAResult(), None, None,
                      {"obsidian": {"vault_path": str(tmp_path)}})

    assert enrich_note(path, ["firm performance"], ["Paper B"], ["Paper C"])
    text = Path(path).read_text(encoding="utf-8")

    assert 'keywords: ["firm performance"]' in text
    assert "[[firm performance]]" in text, "본문 위키링크가 그래프 엣지를 만든다"
    assert "[[Paper B]]" in text
    assert "[[Paper C]]" in text


def test_enrich_is_idempotent(tmp_path):
    """두 번 실행해도 연결 섹션이 중복되면 안 된다."""
    c = PaperCandidate(title="Paper A", year=2024, doi="10.1016/j.a.2024.1")
    v = VerificationResult(candidate=c, status="verified")
    path = write_note(c, v, OAResult(), None, None,
                      {"obsidian": {"vault_path": str(tmp_path)}})
    enrich_note(path, ["kw"], [], [])
    enrich_note(path, ["kw"], [], [])
    assert Path(path).read_text(encoding="utf-8").count("## 연결") == 1


def test_enrich_missing_file_returns_false():
    assert enrich_note("/nonexistent/x.md", [], [], []) is False


def test_empty_records_produce_no_hubs():
    assert build_keyword_index([]) == {}
    assert keywords_for(BUSINESS[0], {}) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
