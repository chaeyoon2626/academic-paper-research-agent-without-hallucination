"""[3] 존재 검증 판정 규칙 테스트.

여기서 확인하는 건 "코드가 돌아가는가"가 아니라 **"판정이 옳은가"**다.
특히 `unavailable`이 `not_found`로 새지 않는지가 핵심이다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core.models import IndexLookup, PaperCandidate  # noqa: E402
from core.verify_paper import Verifier  # noqa: E402


# --- 테스트용 가짜 클라이언트 -------------------------------------------------


class FakeClient:
    def __init__(self, name, outcome, detail="", sim=1.0):
        self.name = name
        self._lookup = IndexLookup(name, outcome, title_similarity=sim, detail=detail,
                                   matched_fields=["identifier", "title", "authors", "year"])
        self.search_results = []

    def lookup(self, cand):
        return self._lookup

    def search(self, query, limit=5):
        return self.search_results


def make_verifier(oa="match", cr="match", ax="match", mode="strict"):
    return Verifier(
        cfg={"verify": {"mode": mode}},
        openalex=FakeClient("openalex", oa),
        crossref=FakeClient("crossref", cr),
        arxiv=FakeClient("arxiv", ax),
    )


CAND_DOI = PaperCandidate(
    title="Attention Is All You Need",
    authors=["Ashish Vaswani", "Noam Shazeer"],
    year=2017,
    doi="10.5555/3295222.3295349",
)


# --- 규칙 1: mismatch가 최우선 ------------------------------------------------


def test_mismatch_wins_over_match():
    """strict 모드: 한 인덱스가 match여도 다른 곳에서 어긋나면 not_found.

    진짜 DOI를 가져다 붙이고 제목만 바꾼 위조가 이 패턴이다.
    """
    v = make_verifier(oa="match", cr="mismatch", mode="strict")
    r = v.verify(CAND_DOI)
    assert r.status == "not_found"
    assert "메타데이터" in r.reason


def test_fast_mode_stops_at_first_match():
    """fast 모드(기본): 첫 확인에서 멈춘다 — 논문당 3초 이상 절약.

    OpenAlex가 match를 준 시점에 '식별자가 실존하고 제목·저자·연도가 실제
    색인 레코드와 일치한다'는 것이 이미 증명됐다. 위조 DOI였다면 OpenAlex
    자신이 mismatch를 냈을 것이다. 인덱스 간 불일치까지 잡으려면 strict를 쓴다.
    """
    v = make_verifier(oa="match", cr="mismatch", mode="fast")
    r = v.verify(CAND_DOI)
    assert r.status == "verified"
    assert len(r.lookups) == 1, "첫 match 이후로는 조회하지 않아야 한다"


def test_fast_mode_still_catches_first_index_mismatch():
    """fast 모드여도 첫 인덱스가 어긋나면 즉시 잡는다."""
    v = make_verifier(oa="mismatch", cr="match", mode="fast")
    r = v.verify(CAND_DOI)
    assert r.status == "not_found"


# --- 규칙 2: match가 absent를 이긴다 ------------------------------------------


def test_match_beats_absent():
    """DOI가 DataCite 등록이면 Crossref는 404를 준다. 이게 부재의 증거가 되면 안 된다."""
    v = make_verifier(oa="match", cr="absent")
    r = v.verify(CAND_DOI)
    assert r.status == "verified"


# --- 규칙 3: 전부 absent면 not_found -------------------------------------------


def test_all_absent_is_not_found():
    v = make_verifier(oa="absent", cr="absent")
    r = v.verify(CAND_DOI)
    assert r.status == "not_found"
    assert "존재하지 않음" in r.reason


# --- 규칙 4: 전부 unavailable이면 uncertain (가장 중요) ------------------------


def test_all_unavailable_is_uncertain_not_notfound():
    """★ API 장애를 '논문 없음'으로 처리하면 네트워크 끊긴 날 전부 가짜가 된다."""
    v = make_verifier(oa="unavailable", cr="unavailable")
    r = v.verify(CAND_DOI)
    assert r.status == "uncertain"
    assert r.status != "not_found"


def test_one_unavailable_one_match_is_verified():
    v = make_verifier(oa="unavailable", cr="match")
    r = v.verify(CAND_DOI)
    assert r.status == "verified"


# --- 식별자 없는 경로 ----------------------------------------------------------


def test_no_identifier_no_search_hit_is_uncertain():
    """미색인 논문(인문학/비영어/오래된 논문)을 not_found로 죽이면 안 된다."""
    v = make_verifier()
    v.openalex.search_results = []
    cand = PaperCandidate(title="어느 오래된 국문 논문", authors=["김철수"], year=1987)
    r = v.verify(cand)
    assert r.status == "uncertain"
    assert "단정하지 않음" in r.reason


def test_no_identifier_good_match_is_verified():
    v = make_verifier()
    v.openalex.search_results = [
        PaperCandidate(
            title="Attention Is All You Need",
            authors=["Ashish Vaswani"],
            year=2017,
            doi="10.5555/x",
            oa_pdf_url="https://example.org/a.pdf",
        )
    ]
    cand = PaperCandidate(title="Attention is all you need", authors=["Vaswani, A."], year=2017)
    r = v.verify(cand)
    assert r.status == "verified"
    # 검색으로 찾은 DOI/PDF가 후보에 채워져야 [5]에서 쓸 수 있다
    assert cand.doi == "10.5555/x"
    assert cand.oa_pdf_url is not None


def test_no_identifier_wrong_author_is_uncertain():
    """제목만 맞고 저자가 다르면 통과시키면 안 된다 (동명/후속 논문 위험)."""
    v = make_verifier()
    v.openalex.search_results = [
        PaperCandidate(title="Deep Learning", authors=["Yann LeCun"], year=2015)
    ]
    cand = PaperCandidate(title="Deep Learning", authors=["Someone Else"], year=2015)
    r = v.verify(cand)
    assert r.status == "uncertain"


# --- 연도 허용 오차 ------------------------------------------------------------


def test_year_off_by_one_is_allowed():
    """온라인 선공개와 지면 게재 연도가 갈리는 건 정상이다."""
    from core.indexes import _compare_against

    cand = PaperCandidate(title="Some Paper", authors=["Kim"], year=2023, doi="10.1/x")
    rec = {"title": "Some Paper", "authors": ["Kim"], "year": 2024}
    assert _compare_against(cand, rec, "crossref").outcome == "match"


def test_year_off_by_three_is_mismatch():
    from core.indexes import _compare_against

    cand = PaperCandidate(title="Some Paper", authors=["Kim"], year=2020, doi="10.1/x")
    rec = {"title": "Some Paper", "authors": ["Kim"], "year": 2024}
    assert _compare_against(cand, rec, "crossref").outcome == "mismatch"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
