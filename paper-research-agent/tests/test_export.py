"""[9-1] Zotero 내보내기 테스트.

핵심 질문:
  1. RIS·BibTeX가 형식적으로 유효한가? (깨진 파일은 Zotero가 통째로 거부한다)
  2. 저자명을 **틀린 순서로 뒤집지 않는가**?
  3. 검증 정보가 태그로 함께 넘어가는가? (없으면 Zotero에서 전부 똑같아 보인다)
"""

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core.export_bib import (  # noqa: E402
    _split_name,
    export_all,
    to_bibtex,
    to_ris,
    write_csv,
)
from core.indexes import CrossrefClient  # noqa: E402
from core.models import (  # noqa: E402
    NoteRecord,
    OAResult,
    PaperCandidate,
    QuoteClaim,
    Summary,
    VerificationResult,
)


def _rec(title, authors, year, doi, venue, depth, fail="none"):
    c = PaperCandidate(title=title, authors=authors, year=year, doi=doi,
                       venue=venue, abstract="R&D spending rose 100% in {2024}.")
    v = VerificationResult(candidate=c, status="verified", reason="openalex에서 확인")
    s = None if depth == "no_summary" else Summary(
        depth=depth, background="배경",
        method="[초록에 없음]" if depth == "abstract_only" else "설문",
        quotes=[QuoteClaim(page=1, quote="q", verified=True)],
    )
    return NoteRecord(candidate=c, verification=v, oa=OAResult("free"), summary=s,
                      note_path=f"/v/{title[:12]}.md", venue_tier="top",
                      fulltext_failure=fail)


RECORDS = [
    _rec("Organizational adoption of AI agents", ["Kim, Minsu", "Lee, Jiwon"],
         2024, "10.1016/j.jbusres.2024.11401", "Journal of Business Research", "full_text"),
    _rec("Generative AI and firm performance", ["Park, Jihoon"],
         2025, "10.1287/mnsc.2025.04812", "Management Science",
         "abstract_only", fail="no_oa_link"),
    _rec("Attention is all you need", ["Vaswani, Ashish"],
         2017, None, "arXiv", "no_summary"),
]


# ---------------------------------------------------------------------------
# 저자명 — 틀린 순서로 뒤집지 않는가
# ---------------------------------------------------------------------------


def test_comma_form_is_split():
    """쉼표는 서지 표준으로 '성, 이름'을 뜻한다. 믿어도 된다."""
    assert _split_name("Lewis, Patrick") == ("Lewis", "Patrick")
    assert _split_name("Kim, Minsu") == ("Kim", "Minsu")


def test_ambiguous_name_is_not_guessed():
    """★ 공백만 있는 이름은 어느 쪽이 성인지 알 수 없다.

    영어권은 뒤가 성("Yann LeCun"), 한국어 로마자는 앞이 성("Kim Minsu").
    추측해서 뒤집으면 인용이 "Minsu, K."처럼 틀리게 나온다.
    확신이 없으면 원문을 그대로 둔다.
    """
    assert _split_name("Kim Minsu") == ("Kim Minsu", "")
    assert _split_name("Yann LeCun") == ("Yann LeCun", "")


def test_crossref_preserves_family_given():
    """★ Crossref는 성/이름을 나눠 준다. 이어붙이면 그 정보가 사라진다."""
    msg = {
        "title": ["T"],
        "author": [{"given": "Minsu", "family": "Kim"},
                   {"family": "Anonymous"},
                   {"name": "Some Consortium"}],
        "issued": {"date-parts": [[2024]]},
    }
    authors = CrossrefClient._parse(msg)["authors"]
    assert authors[0] == "Kim, Minsu", "성/이름 구분이 보존되어야 한다"
    assert _split_name(authors[0]) == ("Kim", "Minsu")
    assert authors[1] == "Anonymous"
    assert authors[2] == "Some Consortium"


def test_empty_name():
    assert _split_name("") == ("", "")
    assert _split_name("   ") == ("", "")


# ---------------------------------------------------------------------------
# RIS 유효성
# ---------------------------------------------------------------------------


def test_ris_records_are_balanced():
    """TY로 열고 ER로 닫는다. 짝이 안 맞으면 Zotero가 파일을 거부한다."""
    ris = to_ris(RECORDS)
    assert ris.count("TY  - ") == len(RECORDS)
    assert ris.count("ER  - ") == len(RECORDS)


def test_ris_line_format():
    """모든 줄이 `XX  - ` 형식이어야 한다."""
    for line in to_ris(RECORDS).splitlines():
        if line.strip():
            assert len(line) >= 6 and line[4:6] == "- ", f"형식 위반: {line!r}"


def test_ris_has_no_raw_newlines_in_fields():
    """RIS는 필드 안에 줄바꿈을 담지 못한다."""
    c = PaperCandidate(title="T", abstract="첫 줄\n둘째 줄", year=2024)
    v = VerificationResult(candidate=c, status="verified", reason="r")
    rec = NoteRecord(candidate=c, verification=v, oa=OAResult(), summary=None, note_path="/x")
    for line in to_ris([rec]).splitlines():
        if line.strip():
            assert len(line) >= 6 and line[4:6] == "- "


def test_ris_carries_verification_tags():
    """★ 검증 정보가 없으면 Zotero에서 전부 똑같아 보인다."""
    ris = to_ris(RECORDS)
    assert "KW  - verify/verified" in ris
    assert "KW  - summary/full_text" in ris
    assert "KW  - summary/abstract_only" in ris
    assert "KW  - method/missing" in ris


def test_ris_includes_uncertain_when_asked():
    unc = [VerificationResult(
        candidate=PaperCandidate(title="어느 국문 논문", year=1998),
        status="uncertain", reason="미색인 가능성")]
    ris = to_ris(RECORDS, uncertain=unc)
    assert ris.count("TY  - ") == len(RECORDS) + 1
    assert "verify/uncertain" in ris


# ---------------------------------------------------------------------------
# BibTeX 유효성
# ---------------------------------------------------------------------------


def test_bibtex_braces_balanced():
    """중괄호가 안 맞으면 LaTeX 빌드가 통째로 깨진다."""
    bib = to_bibtex(RECORDS)
    assert bib.count("{") == bib.count("}")


def test_bibtex_escapes_special_chars():
    """`&`, `%`가 이스케이프되지 않으면 LaTeX이 죽는다."""
    bib = to_bibtex(RECORDS)
    assert r"R\&D" in bib
    assert r"100\%" in bib
    assert "R&D spending" not in bib


def test_bibtex_keys_are_unique():
    """키가 겹치면 BibTeX이 항목 하나를 조용히 버린다."""
    dup = [
        _rec("Organizational adoption of AI agents", ["Kim, Minsu"], 2024,
             "10.1/a", "J", "full_text"),
        _rec("Organizational adoption of AI agents", ["Kim, Minsu"], 2024,
             "10.1/b", "J", "full_text"),
    ]
    keys = re.findall(r"@\w+\{([^,]+),", to_bibtex(dup))
    assert len(keys) == 2
    assert len(set(keys)) == 2, "중복 키가 생겼다"


def test_bibtex_key_format():
    keys = re.findall(r"@\w+\{([^,]+),", to_bibtex(RECORDS))
    assert "kim2024organizational" in keys
    for k in keys:
        assert re.fullmatch(r"[a-z0-9]+", k), f"인용 키에 쓸 수 없는 문자: {k}"


def test_bibtex_authors_joined_with_and():
    bib = to_bibtex(RECORDS)
    assert "Kim, Minsu and Lee, Jiwon" in bib


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_csv_has_bom_for_excel(tmp_path):
    """BOM이 없으면 엑셀이 한글을 깨뜨린다."""
    p = write_csv(RECORDS, str(tmp_path / "x.csv"))
    assert Path(p).read_bytes().startswith(b"\xef\xbb\xbf")


def test_csv_rows_match_records(tmp_path):
    p = write_csv(RECORDS, str(tmp_path / "x.csv"))
    with open(p, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert len(rows) == len(RECORDS) + 1
    assert rows[0][0] == "제목"


# ---------------------------------------------------------------------------
# 통합
# ---------------------------------------------------------------------------


def test_export_all_writes_three_formats(tmp_path):
    paths = export_all("경영학 AI agent", RECORDS,
                       {"obsidian": {"vault_path": str(tmp_path)}})
    assert {Path(p).suffix for p in paths} == {".ris", ".bib", ".csv"}
    for p in paths:
        assert Path(p).stat().st_size > 0


def test_export_disabled(tmp_path):
    assert export_all("q", RECORDS,
                      {"obsidian": {"vault_path": str(tmp_path)},
                       "export": {"enabled": False}}) == []


def test_export_empty_records(tmp_path):
    assert export_all("q", [], {"obsidian": {"vault_path": str(tmp_path)}}) == []


def test_export_filename_is_safe(tmp_path):
    """질문에 슬래시가 들어가도 파일이 만들어져야 한다."""
    paths = export_all('경영학/AI: "agent" 연구?', RECORDS,
                       {"obsidian": {"vault_path": str(tmp_path)}})
    assert paths
    for p in paths:
        assert Path(p).exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
