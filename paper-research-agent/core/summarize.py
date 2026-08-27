"""[7] 원문 근거 요약 생성.

**호출 전제조건: [6]에서 전문 텍스트가 확보된 경우에만 호출한다.**
전제조건은 호출부 책임이지만, 이 모듈도 빈 텍스트를 받으면 예외를 던져
이중으로 막는다. 원칙 2는 조용히 깨지면 안 되는 종류의 원칙이다.

ARS의 Anti-Leakage Protocol(Knowledge Isolation Directive)에서 가져온 것:
모델이 파라메트릭 메모리로 빈칸을 채우는 대신 명시적으로 `[근거 없음]`을
찍게 만든다. "모르면 모른다고 써라"를 프롬프트에 넣는 것만으로는 약하고,
**빈칸을 채울 다른 출구를 만들어줘야** 모델이 지어내지 않는다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core.llm_client import LLMError, call_llm
from core.models import PaperCandidate, QuoteClaim, Summary

log = logging.getLogger(__name__)

DEFAULT_PROMPT_PATH = "prompts/summarize.md"

# (p.12: "인용문") 또는 (p.12: “인용문”)
_QUOTE_RE = re.compile(r'\(\s*p\.\s*(\d+)\s*:\s*["""\u201c]([^"""\u201d]{5,600})["""\u201d]\s*\)')

_SECTION_PATTERNS = {
    "background": r"##\s*(?:배경|Background)",
    "method": r"##\s*(?:방법|Method|Methodology)",
    "key_results_md": r"##\s*(?:핵심 결과|주요 결과|Key Results|Results)",
    "limitations": r"##\s*(?:한계|Limitations)",
    "related_work": r"##\s*(?:관련 연구|Related Work)",
    "conclusion": r"##\s*(?:결론|Conclusion)",
    "open_questions": r"##\s*전문 확인이 필요한 부분",
}

# 초록 기반 인용은 페이지가 없다: ("...") 형태
_ABS_QUOTE_RE = re.compile(r'\(\s*["""\u201c]([^"""\u201d]{5,400})["""\u201d]\s*\)')


class EmptyTextError(Exception):
    """전문 없이 요약을 시도했다. 원칙 2 위반."""


def build_summary_prompt(text: str, cfg: dict) -> str:
    """`prompts/summarize.md`를 읽어 원문 텍스트를 채워 넣는다."""
    if not text or not text.strip():
        raise EmptyTextError(
            "전문 텍스트가 비어 있음 — 요약 프롬프트를 만들지 않음 (원칙 2)"
        )

    path = Path(cfg.get("prompts", {}).get("summarize", DEFAULT_PROMPT_PATH))
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as e:
        raise LLMError(f"요약 프롬프트 파일을 읽을 수 없음: {path} ({e})") from e

    max_chars = int(cfg.get("summarize", {}).get("max_input_chars", 60000))
    body = text if len(text) <= max_chars else text[:max_chars] + "\n\n[...이하 생략...]"

    return template.replace("{{PAPER_TEXT}}", body)


def parse_summary_markdown(raw_md: str) -> Summary:
    """LLM 응답 마크다운을 Summary로. `(p.X: "...")` 패턴을 QuoteClaim으로 뽑는다."""
    summary = Summary(depth="full_text", raw_markdown=raw_md)

    # 섹션 분해
    for field, pattern in _SECTION_PATTERNS.items():
        m = re.search(pattern + r"\s*\n(.*?)(?=\n##\s|\Z)", raw_md, re.DOTALL | re.IGNORECASE)
        if m:
            setattr(summary, field, m.group(1).strip())

    # 인용 추출
    for m in _QUOTE_RE.finditer(raw_md):
        page = int(m.group(1))
        quote = m.group(2).strip()
        # 인용 앞 문장을 claim_text로 (해당 인용이 뒷받침하는 주장)
        start = max(0, m.start() - 400)
        preceding = raw_md[start:m.start()].strip()
        claim = preceding.rsplit("\n", 1)[-1].strip() if preceding else ""
        summary.quotes.append(QuoteClaim(page=page, quote=quote, claim_text=claim[:400]))

    log.info("요약 파싱: 인용 %d건 추출", len(summary.quotes))
    return summary


def summarize(text: str, cfg: dict) -> Summary:
    """전문 텍스트 → 구조화된 요약.

    Raises:
        EmptyTextError: text가 비어 있음 (원칙 2 방어선)
        LLMError:       LLM 호출 실패
    """
    if not text or not text.strip():
        raise EmptyTextError("전문 없이 summarize() 호출됨 — 원칙 2 위반")

    prompt = build_summary_prompt(text, cfg)
    llm = cfg.get("llm", {})

    raw = call_llm(
        prompt=prompt,
        system=(
            "You are a meticulous research assistant. You summarize ONLY from the "
            "text provided in the prompt. You never use prior knowledge about the paper."
        ),
        model=llm.get("model", "ollama/llama3.1"),
        temperature=float(llm.get("summarize_temperature", 0.1)),
        timeout=int(llm.get("timeout", 180)),
        max_tokens=llm.get("max_tokens"),
    )
    return parse_summary_markdown(raw)


# --- [1] 쿼리 확장 -------------------------------------------------------------


def expand_queries(
    question: str,
    cfg: dict,
    exclude: list[str] | None = None,
    vocabulary: list[str] | None = None,
) -> list[str]:
    """[1] 질문 → 검색 쿼리 여러 개.

    LLM이 만드는 건 **키워드뿐**이다. 논문 제목이나 저자를 지어내게 하면 안 된다.
    exclude는 재시도 시 이전 쿼리를 다시 안 쓰게 하는 용도.
    """
    exclude = exclude or []
    path = Path(cfg.get("prompts", {}).get("query_expansion", "prompts/query_expansion.md"))
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as e:
        raise LLMError(f"쿼리 확장 프롬프트를 읽을 수 없음: {path} ({e})") from e

    n = int(cfg.get("search", {}).get("queries_per_round", 4))
    excl = "\n".join(f"- {q}" for q in exclude) if exclude else "(없음)"

    # 시드 논문에서 뽑은 실제 분야 용어. 이게 있으면 LLM이 일반적인 말 대신
    # 그 분야가 쓰는 말로 검색어를 만든다.
    vocab = ", ".join(vocabulary) if vocabulary else "(없음)"

    prompt = (
        template.replace("{{QUESTION}}", question)
        .replace("{{N}}", str(n))
        .replace("{{EXCLUDED}}", excl)
        .replace("{{VOCABULARY}}", vocab)
    )

    llm = cfg.get("llm", {})
    raw = call_llm(
        prompt=prompt,
        system="You output only search keyword lines. No prose, no paper titles, no authors.",
        model=llm.get("model", "ollama/llama3.1"),
        temperature=float(llm.get("query_temperature", 0.4)),
        timeout=int(llm.get("timeout", 120)),
    )

    queries = []
    seen = {q.lower().strip() for q in exclude}
    for line in raw.splitlines():
        q = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"\'')
        if not q or len(q) < 3 or q.lower() in seen:
            continue
        # 모델이 프롬프트를 되풀이하는 경우 방어
        if q.lower().startswith(("here are", "다음은", "search quer")):
            continue
        queries.append(q)
        seen.add(q.lower())

    if not queries:
        log.warning("쿼리 확장 실패 — 원 질문을 그대로 사용")
        return [question]
    return queries[:n]

# --- [3-1] 관련성 판단 -----------------------------------------------------------


def check_relevance(question: str, candidate: PaperCandidate, cfg: dict) -> tuple[bool, str]:
    """[3-1] 존재는 확인된 논문이 질문과 실제로 관련 있는지 판단한다.

    [3]은 '진짜 있는 논문인가'만 본다. 검색이 느슨해서(OpenAlex 전문 검색)
    존재하는데 무관한 논문이 섞여 들어오는 걸 여기서 거른다.
    """
    path = Path(cfg.get("prompts", {}).get("relevance_check", "prompts/relevance_check.md"))
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as e:
        raise LLMError(f"관련성 판단 프롬프트를 읽을 수 없음: {path} ({e})") from e

    prompt = (
        template.replace("{{QUESTION}}", question)
        .replace("{{TITLE}}", candidate.title or "")
        .replace("{{ABSTRACT}}", candidate.abstract or "[초록 없음]")
    )

    llm = cfg.get("llm", {})
    raw = call_llm(
        prompt=prompt,
        system="Output YES or NO on the first line, then one reason line. Nothing else.",
        model=llm.get("model", "ollama/llama3.1"),
        temperature=float(llm.get("relevance_temperature", 0.0)),
        timeout=int(llm.get("timeout", 60)),
    )
    lines = raw.strip().splitlines()
    is_relevant = bool(lines) and lines[0].strip().upper().startswith("YES")
    reason = lines[1].strip() if len(lines) > 1 else ""
    return is_relevant, reason

# --- [7-A] 초록 기반 정리 ------------------------------------------------------

MIN_ABSTRACT_CHARS = 250
"""이보다 짧으면 정리할 내용이 없다. 억지로 요약하면 지어낸다."""


def parse_abstract_markdown(raw_md: str) -> Summary:
    """초록 기반 응답을 파싱. 인용에 페이지가 없다는 점만 다르다."""
    summary = Summary(depth="abstract_only", raw_markdown=raw_md)

    for field, pattern in _SECTION_PATTERNS.items():
        m = re.search(pattern + r"\s*\n(.*?)(?=\n##\s|\Z)", raw_md, re.DOTALL | re.IGNORECASE)
        if m:
            setattr(summary, field, m.group(1).strip())

    for m in _ABS_QUOTE_RE.finditer(raw_md):
        quote = m.group(1).strip()
        start = max(0, m.start() - 300)
        preceding = raw_md[start : m.start()].strip()
        claim = preceding.rsplit("\n", 1)[-1].strip() if preceding else ""
        summary.quotes.append(QuoteClaim(page=None, quote=quote, claim_text=claim[:300]))

    log.info("초록 기반 정리: 인용 %d건", len(summary.quotes))
    return summary


def summarize_abstract(abstract: str, cfg: dict) -> Summary:
    """[7-A] 초록만으로 정리한다.

    **전문 기반 요약과 절대 같은 것으로 취급하지 않는다.** `depth`가
    `abstract_only`로 고정되고, 노트와 UI가 이를 눈에 띄게 표시한다.

    왜 위험한가: 초록에는 방법 상세가 거의 없다. 그런데 "방법을 요약하라"고
    하면 모델은 아는 대로 채운다. 그래서 프롬프트가 `[초록에 없음]`을 명시적
    출구로 주고, 여기서 반환된 인용은 **초록 원문과 문자열 대조**를 거친다.
    지어낸 문장은 대조에서 걸린다.

    Raises:
        EmptyTextError: 초록이 없거나 너무 짧다.
        LLMError:       LLM 호출 실패.
    """
    text = (abstract or "").strip()
    if len(text) < MIN_ABSTRACT_CHARS:
        raise EmptyTextError(
            f"초록이 {len(text)}자뿐이라 정리하지 않음 (기준 {MIN_ABSTRACT_CHARS}자). "
            "짧은 초록을 억지로 늘리면 모델이 지어낸다"
        )

    path = Path(cfg.get("prompts", {}).get("summarize_abstract", "prompts/summarize_abstract.md"))
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as e:
        raise LLMError(f"초록 요약 프롬프트를 읽을 수 없음: {path} ({e})") from e

    llm = cfg.get("llm", {})
    raw = call_llm(
        prompt=template.replace("{{ABSTRACT}}", text),
        system=(
            "You summarize ONLY from the abstract provided. Abstracts rarely contain "
            "method details — when they don't, you write [초록에 없음] rather than "
            "filling in what you assume. You never use prior knowledge about the paper."
        ),
        model=llm.get("model", "ollama/llama3.1"),
        temperature=float(llm.get("summarize_temperature", 0.1)),
        timeout=int(llm.get("timeout", 120)),
        max_tokens=llm.get("max_tokens"),
    )
    return parse_abstract_markdown(raw)
