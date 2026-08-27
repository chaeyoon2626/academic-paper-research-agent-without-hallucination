"""점검에서 실제로 발견한 결함들에 대한 회귀 방지 테스트.

여기 있는 것들은 가정이 아니라 **돌려보고 확인한 실제 문제**다.
같은 게 다시 들어오면 여기서 걸린다.
"""

import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from core.models import (  # noqa: E402
    OAResult,
    PaperCandidate,
    Summary,
    VerificationResult,
)
from core.obsidian_writer import make_filename, write_note  # noqa: E402
from core.runner import _paper_payload, _reject_payload, _verify_many  # noqa: E402
from core.seeds import expand_from_seeds, resolve_seed  # noqa: E402


# ---------------------------------------------------------------------------
# 결함 1: 시드 조회가 터지면 탐색 전체가 죽었다
# ---------------------------------------------------------------------------


class ExplodingClient:
    BASE = "http://fake"

    def search(self, q, limit=5):
        raise ValueError("예상 못 한 오류")

    class http:
        @staticmethod
        def get(*a, **k):
            raise RuntimeError("예상 못 한 오류")


def test_seed_title_search_exception_is_absorbed():
    """시드는 부가 기능이다. 여기서 터져도 본 탐색은 계속돼야 한다."""
    assert resolve_seed("some title", ExplodingClient()) is None


def test_seed_doi_lookup_exception_is_absorbed():
    assert resolve_seed("10.1016/j.test.2023.1", ExplodingClient()) is None


def test_citation_expansion_exception_is_absorbed():
    seed = PaperCandidate(title="t", doi="10.1016/j.test.2023.1")
    assert expand_from_seeds([seed], ExplodingClient()) == []


# ---------------------------------------------------------------------------
# 결함 2: irrelevant 상태가 노트 저장에서 KeyError를 냈다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["verified", "uncertain", "not_found", "irrelevant"])
def test_every_status_can_be_written_to_note(status, tmp_path):
    """상태를 추가하고 저장 쪽을 안 고치면 KeyError로 죽는다."""
    cand = PaperCandidate(title="Some Paper", authors=["Kim"], year=2024)
    vr = VerificationResult(candidate=cand, status=status, reason="사유")
    path = write_note(
        cand, vr, OAResult(), None, None,
        {"obsidian": {"vault_path": str(tmp_path)}},
    )
    assert Path(path).exists()


@pytest.mark.parametrize("status", ["verified", "uncertain", "not_found", "irrelevant"])
def test_every_status_produces_valid_payload(status):
    """UI로 나가는 페이로드도 상태를 가리지 않아야 한다."""
    cand = PaperCandidate(title="T", doi="10.1/x")
    vr = VerificationResult(candidate=cand, status=status, reason="사유")
    p = _reject_payload(vr)
    assert p["status"] == status
    assert "title" in p and "lookups" in p


# ---------------------------------------------------------------------------
# 결함 3: 파일명이 만들어지지 않는 입력
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", ["", "   ", "...", "제목/에\\슬래시:포함", "A" * 300])
def test_filename_is_always_usable(title):
    fn = make_filename(PaperCandidate(title=title))
    assert fn.endswith(".md")
    assert len(fn) < 200
    for ch in '<>:"/\\|?*':
        assert ch not in fn


# ---------------------------------------------------------------------------
# 결함 4: 취소가 검증 중간에 안 먹었다
# ---------------------------------------------------------------------------


class SlowVerifier:
    def verify(self, c):
        time.sleep(0.2)
        return VerificationResult(candidate=c, status="verified")


def test_cancel_stops_verification():
    cancel = threading.Event()
    cancel.set()
    cands = [PaperCandidate(title=f"P{i}") for i in range(8)]
    got = _verify_many(cands, SlowVerifier(), workers=4, cancel=cancel)
    assert len(got) < len(cands), "취소가 걸렸는데 전부 처리했다"


def test_verify_many_preserves_order():
    """결과 순서가 실행마다 달라지면 로그를 비교할 수 없다."""
    cands = [PaperCandidate(title=f"P{i}") for i in range(6)]
    got = _verify_many(cands, SlowVerifier(), workers=3)
    assert [r.candidate.title for r in got] == [c.title for c in cands]


# ---------------------------------------------------------------------------
# 결함 5: 요약이 없는 레코드의 페이로드
# ---------------------------------------------------------------------------


def test_payload_without_summary():
    from core.models import NoteRecord

    cand = PaperCandidate(title="T")
    vr = VerificationResult(candidate=cand, status="verified")
    rec = NoteRecord(candidate=cand, verification=vr, oa=OAResult(),
                     summary=None, note_path="/x")
    p = _paper_payload(rec, vr)
    assert p["summary_depth"] == "no_summary"
    assert p["quotes"] == []


def test_payload_with_summary_but_no_quotes():
    from core.models import NoteRecord

    cand = PaperCandidate(title="T")
    vr = VerificationResult(candidate=cand, status="verified")
    rec = NoteRecord(candidate=cand, verification=vr, oa=OAResult(),
                     summary=Summary(depth="full_text", background="x"),
                     note_path="/x")
    p = _paper_payload(rec, vr)
    assert p["quotes_total"] == 0
    assert p["sections"]["배경"] == "x"


# ---------------------------------------------------------------------------
# 결함 6: CORE가 이상한 응답을 줄 때
# ---------------------------------------------------------------------------


def test_core_rejects_short_text():
    """초록이나 표지만 받아온 걸 전문으로 착각하면 원칙 2가 깨진다."""
    from core.core_client import CoreClient

    c = CoreClient({"core": {"api_key": "k"}})
    assert c._extract_text({"fullText": "짧은 초록"}) is None
    assert c._extract_text({"fullText": None}) is None
    assert c._extract_text({"fullText": 12345}) is None
    assert c._extract_text({}) is None
    assert c._extract_text({"fullText": "x" * 2000}) is not None


def test_core_disabled_without_key():
    """키가 없으면 조용히 꺼지고, 파이프라인은 계속 가야 한다."""
    import os

    saved = os.environ.pop("CORE_API_KEY", None)
    try:
        from core.core_client import CoreClient

        c = CoreClient({"core": {"enabled": True}})
        assert c.enabled is False
        assert c.fetch(PaperCandidate(title="x", doi="10.1/y")) == (None, None)
    finally:
        if saved:
            os.environ["CORE_API_KEY"] = saved


# ---------------------------------------------------------------------------
# 결함 7: 랜딩 페이지에서 PDF를 못 찾았다
# ---------------------------------------------------------------------------


def test_find_pdf_link_from_citation_meta():
    from core.fetch_pdf import find_pdf_link

    html = '<meta name="citation_pdf_url" content="/files/p.pdf">'
    assert find_pdf_link(html, "https://repo.edu/item/1") == "https://repo.edu/files/p.pdf"


def test_find_pdf_link_from_repository_href():
    from core.fetch_pdf import find_pdf_link

    html = '<a href="/bitstream/1/2/thesis.pdf">Download</a>'
    assert find_pdf_link(html, "https://repo.edu/x").endswith("thesis.pdf")


def test_find_pdf_link_returns_none_for_login_page():
    from core.fetch_pdf import find_pdf_link

    assert find_pdf_link("<html>Please sign in</html>", "https://x.com") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# 결함 8: 검색할 때마다 같은 폴더에 결과가 쌓였다
# ---------------------------------------------------------------------------


def test_session_name_has_date_prefix():
    """날짜가 앞에 와야 파일 목록에서 시간순으로 정렬된다."""
    from core.obsidian_writer import make_session_name

    name = make_session_name("경영학 AI agent 활용 사례")
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{4} ", name), name
    assert "경영학" in name


@pytest.mark.parametrize("q", ["", "   ", "RAG/환각: 억제?", "A" * 200])
def test_session_name_is_always_a_usable_folder(q):
    from core.obsidian_writer import make_session_name

    name = make_session_name(q)
    for ch in '<>:"/\\|?*':
        assert ch not in name
    assert name.strip() == name and len(name) < 120


def test_two_searches_do_not_share_a_folder(tmp_path):
    """★ 같은 폴더에 쌓이면 어느 논문이 어느 질문에서 나왔는지 알 수 없다."""
    from core.obsidian_writer import make_session_name, write_note

    paths = []
    for q in ("경영학 AI agent", "RAG 환각 억제"):
        cfg = {"obsidian": {"vault_path": str(tmp_path),
                            "_session_dir": make_session_name(q)}}
        c = PaperCandidate(title="Same Paper Title", authors=["Kim, Minsu"],
                           year=2024, doi="10.1016/j.same.2024.1")
        v = VerificationResult(candidate=c, status="verified", reason="r")
        paths.append(Path(write_note(c, v, OAResult(), None, None, cfg)))

    # 같은 논문이어도 서로 다른 세션 폴더에 들어간다
    assert paths[0].parent != paths[1].parent
    assert paths[0].exists() and paths[1].exists()
    assert "(2)" not in paths[1].name, "중복 번호가 붙었다 = 같은 폴더에 쌓였다"


def test_session_root_falls_back_to_vault(tmp_path):
    """재시도 명령처럼 세션 밖에서 부를 때는 vault 최상위를 쓴다."""
    from core.obsidian_writer import session_root

    assert session_root({"obsidian": {"vault_path": str(tmp_path)}}) == tmp_path


# ---------------------------------------------------------------------------
# 결함 9: 중단이 수십 초씩 걸렸다
# ---------------------------------------------------------------------------


def test_interruptible_sleep_wakes_on_abort():
    """★ arXiv는 3초, 재시도 백오프는 최대 30초를 쉰다.
    그 사이 중단이 안 먹으면 사용자는 먹통이라고 느낀다."""
    from core.http_client import Aborted, interruptible_sleep

    ev = threading.Event()
    threading.Timer(0.15, ev.set).start()
    started = time.monotonic()
    with pytest.raises(Aborted):
        interruptible_sleep(10.0, ev)
    assert time.monotonic() - started < 1.0


def test_interruptible_sleep_completes_without_abort():
    from core.http_client import interruptible_sleep

    started = time.monotonic()
    interruptible_sleep(0.1, threading.Event())
    assert time.monotonic() - started >= 0.09


def test_client_refuses_request_when_already_aborted():
    """중단된 뒤에는 새 요청을 아예 보내지 않는다."""
    from core.http_client import Aborted, ThrottledClient

    ev = threading.Event()
    ev.set()
    client = ThrottledClient("test", min_interval=0, abort=ev)
    with pytest.raises(Aborted):
        client.get("https://example.com")


def test_verify_many_returns_fast_on_cancel():
    """★ 실행 중인 작업의 완료를 기다리면 중단이 수십 초 걸린다."""
    class Slow:
        def verify(self, c):
            time.sleep(0.4)
            return VerificationResult(candidate=c, status="verified")

    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    cands = [PaperCandidate(title=f"P{i}") for i in range(20)]

    started = time.monotonic()
    _verify_many(cands, Slow(), workers=4, cancel=cancel)
    elapsed = time.monotonic() - started

    # 20건을 4개씩 = 5회전 × 0.4초 = 2초. 중단하면 그보다 훨씬 빨라야 한다.
    assert elapsed < 1.2, f"중단에 {elapsed:.1f}초 걸림"
