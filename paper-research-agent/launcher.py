#!/usr/bin/env python3
"""실행 도우미. `start.bat` / `start.command`가 이 파일을 부릅니다.

## 왜 배치 파일이 아니라 파이썬이 이 일을 하는가

윈도우 cmd는 배치 파일을 **현재 코드페이지**로 읽습니다. 한국어 윈도우는
CP949인데 파일이 UTF-8로 저장돼 있으면 한글이 깨지고, 깨진 바이트가
`echo` 같은 명령어까지 망가뜨립니다. `chcp 65001`을 넣으면 파일을 읽는
위치가 어긋나 오히려 더 나빠집니다.

그래서 배치 파일에는 **ASCII만** 두고, 한글 안내는 전부 여기서 출력합니다.
파이썬은 콘솔 인코딩을 알아서 처리하고, CP949는 한글을 문제없이 표현합니다.
"""

from __future__ import annotations

# ── 다른 import보다 먼저 실행되어야 하는 검사 ────────────────────────────
# 표준 모듈을 가리는 파일이 있으면 `import subprocess` 한 줄에서도 인터프리터가
# 죽는다. 그러면 아래쪽에 아무리 친절한 진단을 넣어도 실행될 기회가 없다.
# 그래서 이 검사만은 인터프리터 시작 시 이미 올라와 있는 sys/os만 써서 맨 위에서 한다.
import os
import sys


def _guard_shadowed_stdlib() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    std = getattr(sys, "stdlib_module_names", frozenset())
    try:
        names = os.listdir(here)
    except OSError:
        return

    bad = sorted(
        n for n in names
        if n.endswith(".py") and n[:-3] in std and n != "launcher.py"
    )
    if not bad:
        return

    print()
    print("  [문제] 표준 파이썬 모듈과 이름이 겹치는 파일이 있습니다:")
    print(f"    {', '.join(bad)}")
    print()
    print("  이 파일들이 파이썬 내장 기능을 가려서 프로그램이 동작하지 않습니다.")
    print("  압축을 풀 때 폴더 구조가 사라진 것으로 보입니다.")
    print("  압축 파일을 '폴더 구조 유지' 옵션으로 다시 풀어 주세요.")
    print()
    try:
        input("  Enter 키를 누르면 닫힙니다...")
    except (EOFError, KeyboardInterrupt):
        pass
    raise SystemExit(1)


_guard_shadowed_stdlib()

import subprocess  # noqa: E402 - 위 검사가 먼저 끝나야 안전하다
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).parent.resolve()
MIN_PY = (3, 10)
NEEDED = ["requests", "yaml", "pdfplumber", "pypdf"]  # litellm은 선택 사항


def safe_stdout() -> None:
    """콘솔이 표현하지 못하는 문자가 있어도 죽지 않게."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 못 바꿔도 그냥 진행
            pass


def line(msg: str = "") -> None:
    print(f"  {msg}" if msg else "", flush=True)


def wait_and_exit(code: int) -> None:
    line()
    try:
        input("  Enter 키를 누르면 닫힙니다...")
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(code)


def check_version() -> None:
    if sys.version_info < MIN_PY:
        cur = ".".join(map(str, sys.version_info[:3]))
        line(f"[문제] 파이썬 {MIN_PY[0]}.{MIN_PY[1]} 이상이 필요합니다. 지금은 {cur}입니다.")
        line()
        line("python.org/downloads 에서 최신 버전을 설치하세요.")
        wait_and_exit(1)


def missing_packages() -> list[str]:
    import importlib.util
    return [m for m in NEEDED if importlib.util.find_spec(m) is None]


def install() -> bool:
    """pip 설치. PEP 668로 막히는 환경을 위해 단계적으로 폴백한다."""
    req = ROOT / "requirements.txt"
    if not req.exists():
        line(f"[문제] requirements.txt를 찾을 수 없습니다: {req}")
        line("압축을 푼 폴더에서 실행했는지 확인하세요.")
        return False

    attempts = [
        ([sys.executable, "-m", "pip", "install", "-r", str(req)], "설치 중"),
        ([sys.executable, "-m", "pip", "install", "--user", "-r", str(req)], "사용자 영역에 재시도"),
        ([sys.executable, "-m", "pip", "install", "--break-system-packages", "-r", str(req)],
         "시스템 보호 우회로 재시도"),
    ]
    for cmd, label in attempts:
        line(f"{label}...")
        try:
            if subprocess.call(cmd) == 0:
                return True
        except OSError as e:
            line(f"실행 실패: {e}")
    return False


FILE_HOME: dict[str, set[str]] = {
    "core": {
        "__init__.py", "models.py", "text_similarity.py", "http_client.py", "cache.py",
        "indexes.py", "search_apis.py", "verify_paper.py", "check_venue_tier.py",
        "check_oa.py", "fetch_pdf.py", "parse_pdf.py", "llm_client.py", "summarize.py",
        "verify_quotes.py", "obsidian_writer.py", "runner.py",
    },
    "web": {"index.html"},
    "prompts": {"query_expansion.md", "summarize.md"},
    "tests": {"test_verify_paper.py", "test_verify_quotes.py"},
}
"""파일이 원래 있어야 할 폴더. 자동 복구에 쓴다.

파일을 하나씩 내려받으면 폴더 정보가 사라져 전부 최상위에 쌓인다.
그 상태를 손으로 정리하라고 하는 건 실수하기 쉬우니 여기서 처리한다.
"""

ROOT_FILES = {"launcher.py", "server.py", "main.py", "config.yaml",
              "requirements.txt", "start.bat", "start.command", "README.md"}


def try_repair_layout() -> bool:
    """최상위에 흩어진 파일을 제자리 폴더로 옮긴다. 옮겼으면 True."""
    loose: dict[str, list[str]] = {}
    for folder, names in FILE_HOME.items():
        for n in names:
            src = ROOT / n
            # 최상위에 있고, 제자리에는 아직 없는 파일만 대상
            if src.is_file() and not (ROOT / folder / n).is_file():
                loose.setdefault(folder, []).append(n)

    if not loose:
        return False

    total = sum(len(v) for v in loose.values())
    line()
    line(f"최상위에 흩어진 파일 {total}개를 찾았습니다. 제자리로 옮길 수 있습니다:")
    for folder in sorted(loose):
        line(f"  {folder}\\  ←  {', '.join(sorted(loose[folder]))}")
    line()

    try:
        answer = input("  옮길까요? (Y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer and answer[0] == "n":
        return False

    import shutil

    moved = 0
    for folder, names in loose.items():
        target = ROOT / folder
        try:
            target.mkdir(exist_ok=True)
        except OSError as e:
            line(f"[문제] {folder} 폴더를 만들 수 없습니다: {e}")
            return False
        for n in names:
            try:
                shutil.move(str(ROOT / n), str(target / n))
                moved += 1
            except OSError as e:
                line(f"[문제] {n} 을 옮기지 못했습니다: {e}")

    # 패키지 인식을 위한 빈 __init__.py 보강
    for folder in ("core", "tests"):
        init = ROOT / folder / "__init__.py"
        if (ROOT / folder).is_dir() and not init.exists():
            try:
                init.write_text("", encoding="utf-8")
            except OSError:
                pass

    # data 폴더는 비어 있어도 있어야 한다 (등급 CSV 놓는 자리)
    try:
        (ROOT / "data").mkdir(exist_ok=True)
    except OSError:
        pass

    line()
    line(f"{moved}개 파일을 옮겼습니다.")
    return moved > 0


def check_layout() -> None:
    """폴더 구조가 온전한지 확인하고, 고칠 수 있으면 고친다."""
    required = [
        ("core", "검증·검색 로직"),
        ("web/index.html", "화면"),
        ("prompts", "LLM 프롬프트"),
        ("server.py", "서버"),
        ("requirements.txt", "패키지 목록"),
        ("config.yaml", "설정"),
    ]
    missing = [(p, why) for p, why in required if not (ROOT / p).exists()]
    if not missing:
        return

    # 자동 복구 시도 후 재확인
    if try_repair_layout():
        missing = [(p, why) for p, why in required if not (ROOT / p).exists()]
        if not missing:
            line("폴더 구조를 복구했습니다. 계속 진행합니다.")
            return

    line()
    line("[문제] 필요한 파일이나 폴더를 찾을 수 없습니다:")
    for p, why in missing:
        line(f"  {p}  ({why})")
    line()
    line(f"실행 위치: {ROOT}")
    line()
    line("파일을 하나씩 내려받으면 폴더 구조가 사라집니다.")
    line("ZIP 파일(paper-research-agent.zip)을 받아서 통째로 압축을 풀어 주세요.")
    line()
    line("올바른 구조:")
    line("  start.bat  launcher.py  server.py  config.yaml  requirements.txt")
    line("  core\\   web\\   prompts\\   data\\   tests\\")
    wait_and_exit(1)


def main() -> None:
    safe_stdout()
    line()
    line("논문 탐색 에이전트")
    line("-" * 42)
    line()

    check_version()
    line(f"파이썬 {'.'.join(map(str, sys.version_info[:3]))} 확인")
    check_layout()

    miss = missing_packages()
    if miss:
        line()
        line(f"필요한 패키지가 없습니다: {', '.join(miss)}")
        line("처음 한 번만 설치하며, 인터넷 속도에 따라 몇 분 걸립니다.")
        line()
        if not install():
            line()
            line("[문제] 패키지 설치에 실패했습니다.")
            line("인터넷 연결을 확인하세요. 회사/학교 네트워크면 방화벽일 수 있습니다.")
            line()
            line("직접 설치하려면:")
            line(f"  {Path(sys.executable).name} -m pip install -r requirements.txt")
            wait_and_exit(1)

        still = missing_packages()
        if still:
            line()
            line(f"[문제] 설치 후에도 불러올 수 없는 패키지: {', '.join(still)}")
            line("창을 닫고 다시 실행해 보세요.")
            wait_and_exit(1)
        line()
        line("설치를 마쳤습니다.")
    else:
        line("패키지가 모두 준비되어 있습니다.")

    line()
    line("브라우저가 곧 열립니다. 이 창을 닫으면 종료됩니다.")
    line("-" * 42)

    sys.path.insert(0, str(ROOT))
    try:
        import server
    except Exception as e:  # noqa: BLE001
        line()
        line(f"[문제] 서버를 불러오지 못했습니다: {type(e).__name__}: {e}")
        wait_and_exit(1)

    try:
        server.main()
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        if e.code not in (0, None):
            line()
            line(str(e.code))
            wait_and_exit(1)
    except Exception as e:  # noqa: BLE001
        line()
        line(f"[문제] 서버가 예기치 않게 종료되었습니다: {type(e).__name__}: {e}")
        wait_and_exit(1)


if __name__ == "__main__":
    main()
