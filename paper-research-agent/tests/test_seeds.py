"""[0-1] 시드 논문 테스트.

핵심 질문:
  1. 오타나 없는 논문을 넣었을 때 **엉뚱한 논문을 시드로 채택하지 않는가?**
     (시드가 틀리면 그 논문의 어휘와 인용 그래프를 따라가므로 탐색 전체가 어긋난다)
  2. 분야 용어를 실제로 뽑아내는가?
  3. 시드가 없거나 실패해도 파이프라인이 계속 가는가?
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core.models import PaperCandidate  # noqa: E402
from core.seeds import (  # noqa: E402
    extract_vocabulary,
    resolve_seed,
    resolve_seeds,
    seed_venues,
)


class FakeClient:
    """제목 검색만 흉내내는 최소 클라이언트."""

    BASE = "http://fake/works"

    def __init__(self, results=None):
        self.results = results or []

    def search(self, query, limit=5):
        return self.results


SEED_A = PaperCandidate(
    title="Organizational adoption of AI agents in managerial decision making",
    abstract="We examine how firms adopt autonomous agents. Organizational adoption "
             "depends on managerial decision making and firm performance outcomes.",
    year=2023, doi="10.1016/j.jbusres.2023.11401", venue="Journal of Business Research",
)
SEED_B = PaperCandidate(
    title="Generative AI and firm performance: organizational adoption evidence",
    abstract="Firm performance effects of generative AI adoption across organizations. "
             "Managerial decision making shifts with agent deployment.",
    year=2024, doi="10.1016/j.jbusres.2024.22202", venue="Journal of Business Research",
)


# --- 시드 해석: 틀린 입력을 추측하지 않는가 -------------------------------------


def test_wrong_title_is_not_guessed():
    """★ 제목이 충분히 안 맞으면 시드로 채택하면 안 된다.

    시드가 틀리면 그 논문의 어휘와 인용 그래프를 따라가므로 탐색 전체가
    어긋난다. '비슷한 걸 골라주기'보다 '못 찾았다'가 훨씬 안전하다.
    """
    client = FakeClient([SEED_A])
    got = resolve_seed("Quantum chromodynamics lattice simulation methods", client)
    assert got is None


def test_close_enough_title_is_accepted():
    client = FakeClient([SEED_A])
    got = resolve_seed("Organizational adoption of AI agents in managerial decision", client)
    assert got is SEED_A


def test_empty_input_returns_none():
    client = FakeClient([SEED_A])
    assert resolve_seed("", client) is None
    assert resolve_seed("   ", client) is None


def test_no_search_results():
    assert resolve_seed("anything", FakeClient([])) is None


def test_resolve_seeds_separates_found_and_missing():
    client = FakeClient([SEED_A])
    found, missing = resolve_seeds(
        ["Organizational adoption of AI agents in managerial decision making",
         "Totally unrelated astrophysics paper",
         "  "],
        client,
    )
    assert len(found) == 1
    assert missing == ["Totally unrelated astrophysics paper"]


def test_seed_count_is_capped():
    """사용자가 20개를 붙여넣어도 상한을 지켜야 한다 (API 부하)."""
    client = FakeClient([SEED_A])
    title = SEED_A.title
    found, _ = resolve_seeds([title] * 20, client)
    assert len(found) <= 5


# --- 분야 어휘 -----------------------------------------------------------------


def test_vocabulary_extracts_domain_terms():
    """★ 그 분야가 실제로 쓰는 표현이 나와야 한다."""
    vocab = extract_vocabulary([SEED_A, SEED_B])
    joined = " ".join(vocab)
    assert "organizational adoption" in joined
    assert "firm performance" in joined


def test_vocabulary_prefers_phrases_over_single_words():
    """'organizational adoption'이 'adoption' 하나보다 검색어로 쓸모 있다."""
    vocab = extract_vocabulary([SEED_A, SEED_B])
    assert any(" " in v for v in vocab[:5])


def test_vocabulary_drops_generic_words():
    vocab = extract_vocabulary([SEED_A, SEED_B])
    for junk in ("the", "study", "research", "analysis", "we"):
        assert junk not in vocab


def test_vocabulary_empty_seeds():
    assert extract_vocabulary([]) == []


def test_vocabulary_handles_missing_abstract():
    bare = PaperCandidate(title="Organizational adoption of agents")
    assert extract_vocabulary([bare, bare]) != []


def test_seed_venues():
    assert seed_venues([SEED_A, SEED_B]) == [
        "Journal of Business Research", "Journal of Business Research"
    ]
    assert seed_venues([PaperCandidate(title="x")]) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
