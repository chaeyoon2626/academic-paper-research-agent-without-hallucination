#!/usr/bin/env python3
"""할루시네이션 없는 논문 탐색·정리 에이전트 — CLI.

웹 UI를 쓰려면 `python server.py`를 실행하세요.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from core.runner import resummarize_from_url, run_pipeline

log = logging.getLogger("main")


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"설정 파일이 없습니다: {path}")
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for noisy in ("urllib3", "pdfminer", "pdfplumber", "LiteLLM", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_ICON = {"verified": "✅", "uncertain": "❓", "not_found": "❌"}


def console_printer(kind: str, payload: dict) -> None:
    if kind == "log":
        prefix = {"error": "  !", "warn": "  ~"}.get(payload.get("level", ""), "  ·")
        print(f"{prefix} {payload['text']}", flush=True)

    elif kind == "query":
        print(f"\n[{payload['round']}회차] 쿼리: {', '.join(payload['queries'])}", flush=True)

    elif kind == "paper":
        icon = _ICON[payload["status"]]
        title = payload["title"]
        title = title[:64] + ("…" if len(title) > 64 else "")
        line = f"    {icon} {title}"
        if payload["status"] == "verified":
            if payload["summary_depth"] == "full_text":
                line += f"  [요약 완료, 인용 {payload['quotes_verified']}/{payload['quotes_total']}]"
            else:
                line += f"  [요약 불가 — OA: {payload['oa_status']}]"
        else:
            line += f"  — {payload['reason']}"
        print(line, flush=True)

    elif kind == "done":
        print("\n" + "-" * 60, flush=True)
        print(
            f"검증 통과 {payload['verified']}건 "
            f"(전문 요약 {payload['summarized']}건 / "
            f"요약 불가 {payload['verified'] - payload['summarized']}건)",
            flush=True,
        )
        print(f"불확실 {payload['uncertain']}건 / 확인 실패 {payload['not_found']}건", flush=True)
        print(f"\n세션 노트: {payload['moc_path']}", flush=True)
        print(f"실행 로그: {payload['log_path']}", flush=True)


def main(question: str, config_path: str = "config.yaml", verbose: bool = False,
         seeds: list[str] | None = None) -> None:
    cfg = load_config(config_path)
    setup_logging(verbose)
    print(f"\n질문: {question}")
    print(f"모델: {cfg.get('llm', {}).get('model', '(미설정)')}")
    run_pipeline(question, cfg, on_event=console_printer, seeds=seeds)


def cli() -> None:
    ap = argparse.ArgumentParser(
        description="할루시네이션 없는 논문 탐색·정리 에이전트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='예: python main.py "RAG 시스템의 환각 억제 기법"\n웹 UI: python server.py',
    )
    ap.add_argument("question", nargs="?", help="연구 질문 (재시도 모드에서는 생략)")
    ap.add_argument("-c", "--config", default="config.yaml", help="설정 파일 경로")
    ap.add_argument("-v", "--verbose", action="store_true", help="디버그 로그")
    ap.add_argument("--seed", action="append", default=[], metavar="DOI|제목",
                    help="확실히 관련 있는 논문 (여러 번 지정 가능, 최대 5개)")
    ap.add_argument("--retry-doi", help="구글 스칼라 등에서 PDF를 직접 찾은 논문의 DOI")
    ap.add_argument("--retry-pdf", help="그 논문의 PDF URL 또는 로컬 파일 경로")
    args = ap.parse_args()

    if args.retry_doi and args.retry_pdf:
        cfg = load_config(args.config)
        setup_logging(args.verbose)
        resummarize_from_url(args.retry_doi, args.retry_pdf, cfg, on_event=console_printer)
        return

    if not args.question:
        ap.error("question이 필요합니다 (또는 --retry-doi/--retry-pdf 둘 다 지정)")
    main(args.question, args.config, args.verbose, args.seed)


if __name__ == "__main__":
    cli()
