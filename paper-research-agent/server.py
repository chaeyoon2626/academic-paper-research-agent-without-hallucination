#!/usr/bin/env python3
"""로컬 웹 서버. `python server.py` → 브라우저가 열립니다.

의존성은 파이썬 표준 라이브러리 + 기존 requirements뿐입니다. 웹 프레임워크를
추가로 설치할 필요가 없습니다.

## 왜 서버가 필요한가 (브라우저만으로 안 되는 이유)

1. **PDF 다운로드** — 브라우저는 CORS 때문에 임의 서버의 PDF를 못 읽습니다.
   OA PDF는 수백 개 리포지토리에 흩어져 있고 그쪽이 CORS 헤더를 줄 이유가
   없습니다. 전문을 못 받으면 모든 논문이 "요약 불가"가 되어 프로젝트가
   무의미해집니다.
2. **파일 저장** — 브라우저는 옵시디언 vault 경로에 직접 못 씁니다.
3. **API 키** — 브라우저에 키를 두면 확장 프로그램·개발자도구에 노출됩니다.
   여기서는 키가 로컬 `.env`에만 남고 브라우저로는 마스킹된 값만 갑니다.

## 보안

기본으로 **127.0.0.1에만 바인딩**합니다. 같은 네트워크의 다른 기기에서
접근할 수 없습니다. `--host 0.0.0.0`은 이 서버가 로컬 파일시스템에 쓰기
권한을 가지므로 권장하지 않습니다.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

from core.runner import run_pipeline

log = logging.getLogger("server")

ROOT = Path(__file__).parent.resolve()
WEB_DIR = ROOT / "web"
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"

MAX_BODY = 2 * 1024 * 1024  # 2MB. 이보다 큰 POST는 받을 이유가 없다.

# 공급자 → litellm 모델 문자열 + 환경변수 이름
PROVIDERS = {
    "anthropic": {"env": "ANTHROPIC_API_KEY", "label": "Anthropic",
                  "models": ["anthropic/claude-sonnet-4-5", "anthropic/claude-opus-4-1",
                             "anthropic/claude-haiku-4-5"]},
    "openai": {"env": "OPENAI_API_KEY", "label": "OpenAI",
               "models": ["gpt-4o", "gpt-4o-mini", "o3-mini"]},
    "gemini": {"env": "GEMINI_API_KEY", "label": "Google Gemini",
               "models": ["gemini/gemini-2.0-flash", "gemini/gemini-1.5-pro"]},
    "ollama": {"env": "", "label": "Ollama (로컬)",
               "models": ["ollama/llama3.1", "ollama/qwen2.5", "ollama/gemma2"]},
}


# ---------------------------------------------------------------------------
# 작업 관리
# ---------------------------------------------------------------------------


class Job:
    def __init__(self, job_id: str, question: str, seeds: list[str] | None = None):
        self.id = job_id
        self.question = question
        self.seeds = seeds or []
        self.events: list[dict] = []
        self.state = "running"       # running | done | error | cancelled
        self.error = ""
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.started = time.time()
        self.note_dir: str = ""

    def add(self, kind: str, payload: dict) -> None:
        with self.lock:
            self.events.append({"kind": kind, "payload": payload, "t": time.time()})

    def since(self, cursor: int) -> tuple[list[dict], int]:
        with self.lock:
            return self.events[cursor:], len(self.events)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def run_job(job: Job, cfg: dict) -> None:
    """작업 스레드. **어떤 경우에도 job.state를 running으로 남기지 않는다.**

    state가 running으로 굳으면 UI는 완료 신호를 영영 못 받고 폴링만 계속한다.
    사용자 눈에는 "서버가 죽었다"로 보인다. 실제로는 스레드 하나만 죽은 것이라
    서버는 멀쩡한데, 화면이 멈춰 있으니 구분할 방법이 없다.
    """
    try:
        run_pipeline(job.question, cfg, on_event=job.add,
                     cancel=job.cancel, seeds=job.seeds)
        job.state = "cancelled" if job.cancel.is_set() else "done"
    except BaseException as e:  # noqa: BLE001 — KeyboardInterrupt/SystemExit까지 포함
        log.exception("작업 실패")
        job.state = "cancelled" if job.cancel.is_set() else "error"
        job.error = f"{type(e).__name__}: {e}"
        job.add("log", {"text": f"실행 중단: {job.error}", "level": "error"})
    finally:
        # 위 분기가 전부 빗나가도 running만은 아니게 한다
        if job.state == "running":
            job.state = "done"
        job.add("done_marker", {})


# ---------------------------------------------------------------------------
# 설정 읽기/쓰기
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_env(updates: dict[str, str]) -> None:
    """키를 .env에 병합 저장하고 현재 프로세스에도 반영."""
    env = read_env()
    env.update({k: v for k, v in updates.items() if v})
    lines = ["# 로컬 전용. 절대 커밋하지 마세요.", ""]
    lines += [f"{k}={v}" for k, v in sorted(env.items())]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV_PATH.chmod(0o600)  # 소유자만 읽기
    except OSError:
        pass
    for k, v in env.items():
        if v:
            os.environ[k] = v


def mask(secret: str) -> str:
    if not secret:
        return ""
    return f"{secret[:6]}{'•' * 12}{secret[-4:]}" if len(secret) > 14 else "•" * len(secret)


def is_obsidian_vault(path: str) -> bool:
    """`.obsidian` 폴더가 있으면 실제 vault다."""
    try:
        return (Path(path).expanduser() / ".obsidian").is_dir()
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 핸들러
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "paper-agent/0.1"

    def log_message(self, fmt: str, *args) -> None:  # 기본 로그가 시끄럽다
        if os.environ.get("PAPER_AGENT_DEBUG"):
            super().log_message(fmt, *args)

    # -- 응답 헬퍼 -----------------------------------------------------------

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg: str, status: int = 400) -> None:
        self._json({"error": msg}, status)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- 정적 파일 -----------------------------------------------------------

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        # 경로 탈출 방지
        if not str(target).startswith(str(WEB_DIR)) or not target.is_file():
            self._err("찾을 수 없습니다", 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if "text" in ctype or "javascript" in ctype else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- GET -----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/api/config":
            self._get_config()
        elif path.startswith("/api/job/"):
            self._get_job(path)
        elif path.startswith("/api/download/"):
            self._download(path)
        else:
            self._static(path)

    def _get_config(self) -> None:
        cfg = load_config()
        env = read_env()
        obs = cfg.get("obsidian", {})
        vault = obs.get("vault_path", "./vault")

        # 현재 모델 문자열에서 공급자 추론
        model = cfg.get("llm", {}).get("model", "ollama/llama3.1")
        provider = "ollama"
        for name in PROVIDERS:
            if model.startswith(name + "/") or (name == "openai" and model.startswith(("gpt-", "o1", "o3"))):
                provider = name
                break

        self._json({
            "providers": PROVIDERS,
            "provider": provider,
            "model": model,
            "key_set": {p: bool(env.get(d["env"])) for p, d in PROVIDERS.items() if d["env"]},
            "key_masked": {
                p: mask(env.get(d["env"], "")) for p, d in PROVIDERS.items() if d["env"]
            },
            "contact_email": cfg.get("contact_email", ""),
            "search": cfg.get("search", {}),
            "verify": cfg.get("verify", {}),
            "theme": cfg.get("ui", {}).get("theme", "auto"),
            "core_key_set": bool(env.get("CORE_API_KEY")),
            "core_key_masked": mask(env.get("CORE_API_KEY", "")),
            "abstract_fallback": cfg.get("summarize", {}).get("abstract_fallback", True),
            "fetch_fulltext": cfg.get("summarize", {}).get("fetch_fulltext", True),
            "graph": cfg.get("graph", {}),
            "export": cfg.get("export", {}),
            "obsidian_enabled": bool(obs.get("enabled", True)),
            "vault_path": vault,
            "vault_is_obsidian": is_obsidian_vault(vault),
            "output_dir": cfg.get("paths", {}).get("output_dir", "./output"),
        })

    def _get_job(self, path: str) -> None:
        m = re.match(r"^/api/job/([\w-]+)$", path)
        if not m:
            self._err("작업 ID 형식이 올바르지 않습니다", 400)
            return
        job = JOBS.get(m.group(1))
        if job is None:
            self._err("작업을 찾을 수 없습니다", 404)
            return
        try:
            cursor = int(urlparse(self.path).query.split("cursor=")[-1].split("&")[0])
        except (ValueError, IndexError):
            cursor = 0
        events, total = job.since(cursor)
        self._json({
            "state": job.state, "error": job.error, "cursor": total,
            "events": events, "elapsed": round(time.time() - job.started, 1),
        })

    def _download(self, path: str) -> None:
        """생성된 노트를 ZIP으로. 옵시디언 연동을 끈 경우의 산출물 전달 경로."""
        m = re.match(r"^/api/download/([\w-]+)$", path)
        if not m or m.group(1) not in JOBS:
            self._err("작업을 찾을 수 없습니다", 404)
            return

        cfg = load_config()
        vault = Path(cfg.get("obsidian", {}).get("vault_path", "./vault")).expanduser()
        if not vault.is_dir():
            self._err("저장 폴더가 아직 없습니다", 404)
            return

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in vault.rglob("*.md"):
                z.write(f, f.relative_to(vault))
        data = buf.getvalue()

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="paper-notes.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- POST ----------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/config":
            self._save_config()
        elif path == "/api/check-path":
            self._check_path()
        elif path == "/api/run":
            self._run()
        elif path.startswith("/api/cancel/"):
            self._cancel(path)
        else:
            self._err("없는 주소입니다", 404)

    def _save_config(self) -> None:
        b = self._body()
        cfg = load_config()

        if b.get("model"):
            cfg.setdefault("llm", {})["model"] = b["model"]
        if "contact_email" in b:
            cfg["contact_email"] = b["contact_email"]

        for key in ("target_verified", "max_rounds", "per_source_limit", "queries_per_round"):
            if key in b:
                try:
                    cfg.setdefault("search", {})[key] = max(1, int(b[key]))
                except (TypeError, ValueError):
                    pass
        if "use_arxiv" in b:
            cfg.setdefault("search", {})["use_arxiv"] = bool(b["use_arxiv"])
        if "title_similarity_threshold" in b:
            try:
                v = float(b["title_similarity_threshold"])
                cfg.setdefault("verify", {})["title_similarity_threshold"] = min(max(v, 0.3), 1.0)
            except (TypeError, ValueError):
                pass

        if b.get("theme") in ("auto", "light", "dark"):
            cfg.setdefault("ui", {})["theme"] = b["theme"]
        if b.get("verify_mode") in ("fast", "strict"):
            cfg.setdefault("verify", {})["mode"] = b["verify_mode"]

        for key, section, name in (
            ("use_arxiv", "search", "use_arxiv"),
            ("fetch_fulltext", "summarize", "fetch_fulltext"),
            ("abstract_fallback", "summarize", "abstract_fallback"),
            ("graph_enabled", "graph", "enabled"),
            ("citation_edges", "graph", "citation_edges"),
            ("export_enabled", "export", "enabled"),
            ("export_include_uncertain", "export", "include_uncertain"),
        ):
            if key in b:
                cfg.setdefault(section, {})[name] = bool(b[key])

        if b.get("core_api_key"):
            write_env({"CORE_API_KEY": b["core_api_key"]})

        # 옵시디언 연동 토글 — 켜면 vault 경로, 끄면 로컬 output 폴더.
        # 어느 쪽이든 산출물은 동일한 마크다운이다.
        obs = cfg.setdefault("obsidian", {})
        enabled = bool(b.get("obsidian_enabled", True))
        obs["enabled"] = enabled
        if enabled and b.get("vault_path"):
            obs["vault_path"] = b["vault_path"]
        elif not enabled:
            out = b.get("output_dir") or "./output"
            cfg.setdefault("paths", {})["output_dir"] = out
            obs["vault_path"] = out

        save_config(cfg)

        # API 키는 config.yaml이 아니라 .env로 (실수로 커밋되는 사고 방지)
        if b.get("api_key") and b.get("provider") in PROVIDERS:
            envname = PROVIDERS[b["provider"]]["env"]
            if envname:
                write_env({envname: b["api_key"]})

        self._json({"ok": True})

    def _check_path(self) -> None:
        """경로가 쓸 수 있는 곳인지, 옵시디언 vault인지 확인."""
        raw = (self._body().get("path") or "").strip()
        if not raw:
            self._json({"ok": False, "message": "경로를 입력하세요"})
            return
        p = Path(raw).expanduser()
        if p.exists() and not p.is_dir():
            self._json({"ok": False, "message": "폴더가 아니라 파일입니다"})
            return
        if p.is_dir():
            writable = os.access(p, os.W_OK)
            self._json({
                "ok": writable,
                "is_vault": is_obsidian_vault(str(p)),
                "resolved": str(p.resolve()),
                "message": "폴더를 찾았습니다" if writable else "쓰기 권한이 없습니다",
            })
            return
        # 없는 폴더 — 만들 수 있는지 부모로 판단
        parent = p.parent
        can = parent.is_dir() and os.access(parent, os.W_OK)
        self._json({
            "ok": can, "is_vault": False, "resolved": str(p),
            "message": "실행할 때 새로 만듭니다" if can else "상위 폴더가 없거나 쓸 수 없습니다",
        })

    def _run(self) -> None:
        b = self._body()
        question = (b.get("question") or "").strip()
        if not question:
            self._err("연구 질문을 입력하세요")
            return
        if len(question) > 2000:
            self._err("질문이 너무 깁니다 (2000자 이내)")
            return

        cfg = load_config()
        for k, v in read_env().items():  # 키를 현재 프로세스로
            if v:
                os.environ.setdefault(k, v)

        raw_seeds = b.get("seeds") or []
        if not isinstance(raw_seeds, list):
            raw_seeds = []
        # 사용자 입력이므로 개수와 길이를 제한한다
        seeds = [str(x).strip()[:400] for x in raw_seeds if str(x).strip()][:5]

        job = Job(uuid.uuid4().hex[:12], question, seeds)
        with JOBS_LOCK:
            JOBS[job.id] = job
        threading.Thread(target=run_job, args=(job, cfg), daemon=True).start()
        self._json({"job_id": job.id})

    def _cancel(self, path: str) -> None:
        m = re.match(r"^/api/cancel/([\w-]+)$", path)
        job = JOBS.get(m.group(1)) if m else None
        if job is None:
            self._err("작업을 찾을 수 없습니다", 404)
            return
        job.cancel.set()
        self._json({"ok": True})


# ---------------------------------------------------------------------------


def start_server(host: str, port: int, tries: int = 12):
    """포트가 이미 쓰이면 다음 포트로 넘어간다.

    런처를 두 번 눌렀거나 이전 창이 안 닫혔을 때 '주소가 이미 사용 중'이라는
    영문 예외만 뜨고 끝나면 원인을 알기 어렵다. 그냥 옆 포트로 뜨는 게 낫다.
    """
    last: OSError | None = None
    for i in range(tries):
        try:
            srv = ThreadingHTTPServer((host, port + i), Handler)
            srv.daemon_threads = True
            return srv, port + i
        except OSError as e:
            last = e
            continue
    raise SystemExit(
        f"{port}~{port + tries - 1} 포트가 모두 사용 중입니다.\n"
        f"실행 중인 다른 창을 닫고 다시 시도하세요. ({last})"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="논문 탐색 에이전트 웹 UI")
    ap.add_argument("-p", "--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="기본 127.0.0.1. 이 서버는 로컬 파일에 쓰므로 외부 공개를 권장하지 않습니다")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)s: %(message)s")
    for noisy in ("urllib3", "pdfminer", "pdfplumber", "LiteLLM", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not (WEB_DIR / "index.html").exists():
        raise SystemExit(f"web/index.html이 없습니다: {WEB_DIR}")

    srv, port = start_server(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    print(f"\n  논문 탐색 에이전트")
    print(f"  {url}")
    if port != args.port:
        print(f"  ({args.port}번 포트가 사용 중이라 {port}번으로 열었습니다)")
    print()
    if args.host != "127.0.0.1":
        print("  ! 이 서버는 로컬 파일시스템에 씁니다. 신뢰할 수 있는 네트워크에서만 쓰세요.\n")
    print("  이 창을 닫으면 종료됩니다. 종료: Ctrl+C\n")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료합니다.")
        srv.shutdown()


if __name__ == "__main__":
    main()
