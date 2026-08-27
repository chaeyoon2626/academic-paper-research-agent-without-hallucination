"""LLM 호출. **LLM 공급자를 아는 파일은 여기 하나뿐이다.**

## 왜 litellm을 안 쓰는가

원래 litellm으로 공급자를 추상화했는데, 그게 pydantic·tokenizers·aiohttp 등
13개 패키지를 끌고 온다. 정작 우리가 쓰는 건 "메시지 보내고 텍스트 받기"
하나뿐이다. 실제로 파이썬 3.13에서 `pydantic_core` 컴파일 실패로 프로그램
전체가 못 뜨는 일이 있었다. **쓰지도 않는 기능 때문에 못 켜지는 건 손해다.**

그래서 각 공급자의 REST API를 `requests`로 직접 부른다. 네 곳 다 요청 형식이
단순해서, 전부 합쳐도 litellm 어댑터 한 개보다 짧다.

litellm이 설치돼 있으면 여기서 모르는 모델 문자열은 litellm에 넘긴다.
있으면 쓰고 없으면 그만인 선택 사항이다.

## 모델 문자열

    openai/gpt-4o   또는  gpt-4o
    anthropic/claude-sonnet-4-5
    gemini/gemini-2.0-flash
    ollama/llama3.1
"""

from __future__ import annotations

import logging
import os
import random
import time

import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF = 2.0
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMError(Exception):
    """LLM 호출 실패. 호출부는 이걸 잡아서 '요약 불가'로 처리해야 한다."""


# ---------------------------------------------------------------------------
# 모델 문자열 해석
# ---------------------------------------------------------------------------


def split_model(model: str) -> tuple[str, str]:
    """'anthropic/claude-sonnet-4-5' → ('anthropic', 'claude-sonnet-4-5')

    접두사가 없으면 이름 규칙으로 추론한다.
    """
    m = (model or "").strip()
    if "/" in m:
        prefix, rest = m.split("/", 1)
        p = prefix.lower()
        if p in ("openai", "anthropic", "gemini", "google", "ollama"):
            return ("gemini" if p == "google" else p), rest

    low = m.lower()
    if low.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai", m
    if low.startswith("claude"):
        return "anthropic", m
    if low.startswith("gemini"):
        return "gemini", m
    return "unknown", m


def _need_key(env_name: str, provider: str) -> str:
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise LLMError(
            f"{provider} API 키가 없습니다. 설정 화면에서 키를 입력하고 "
            f"'설정 저장'을 누른 뒤 서버를 다시 시작하세요. (환경변수 {env_name})"
        )
    return key


# ---------------------------------------------------------------------------
# 공급자별 요청
# ---------------------------------------------------------------------------


def _post(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    """POST 1회. 재시도는 상위에서 한다."""
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise _Retryable(f"네트워크 오류: {e}") from e

    if r.status_code in RETRYABLE_STATUS:
        raise _Retryable(f"HTTP {r.status_code}: {r.text[:200]}")
    if not r.ok:
        # 401/403/404는 재시도해도 같다. 원인을 그대로 보여준다.
        raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")

    try:
        return r.json()
    except ValueError as e:
        raise LLMError(f"응답을 해석할 수 없습니다: {e}") from e


class _Retryable(Exception):
    """재시도할 가치가 있는 실패."""


def _call_openai(model: str, system: str | None, prompt: str,
                 temperature: float, timeout: int, max_tokens: int | None) -> str:
    key = _need_key("OPENAI_API_KEY", "OpenAI")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    payload: dict = {"model": model, "messages": msgs}
    # o1/o3 계열은 temperature를 받지 않는다
    if not model.lower().startswith(("o1", "o3", "o4")):
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_completion_tokens"] = max_tokens

    data = _post(f"{base}/chat/completions",
                 {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                 payload, timeout)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"예상과 다른 응답 형식: {e}") from e


def _call_anthropic(model: str, system: str | None, prompt: str,
                    temperature: float, timeout: int, max_tokens: int | None) -> str:
    key = _need_key("ANTHROPIC_API_KEY", "Anthropic")
    payload: dict = {
        "model": model,
        "max_tokens": max_tokens or 4096,   # Anthropic은 필수 항목
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    data = _post("https://api.anthropic.com/v1/messages",
                 {"x-api-key": key, "anthropic-version": "2023-06-01",
                  "Content-Type": "application/json"},
                 payload, timeout)
    try:
        # content는 블록 배열이다. 텍스트 블록만 이어붙인다.
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
    except (AttributeError, TypeError) as e:
        raise LLMError(f"예상과 다른 응답 형식: {e}") from e


def _call_gemini(model: str, system: str | None, prompt: str,
                 temperature: float, timeout: int, max_tokens: int | None) -> str:
    key = _need_key("GEMINI_API_KEY", "Gemini")
    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if max_tokens:
        payload["generationConfig"]["maxOutputTokens"] = max_tokens

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    data = _post(url, {"Content-Type": "application/json"}, payload, timeout)
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"예상과 다른 응답 형식: {e}") from e


def _call_ollama(model: str, system: str | None, prompt: str,
                 temperature: float, timeout: int, max_tokens: int | None) -> str:
    base = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434").rstrip("/")
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    payload: dict = {
        "model": model, "messages": msgs, "stream": False,
        "options": {"temperature": temperature},
    }
    if max_tokens:
        payload["options"]["num_predict"] = max_tokens

    try:
        data = _post(f"{base}/api/chat", {"Content-Type": "application/json"},
                     payload, timeout)
    except _Retryable as e:
        # 연결 실패는 대개 Ollama가 안 켜진 것이다. 재시도해도 소용없다.
        if "네트워크" in str(e):
            raise LLMError(
                f"Ollama에 연결할 수 없습니다 ({base}). "
                "Ollama가 실행 중인지 확인하세요. 설치: https://ollama.com"
            ) from e
        raise
    try:
        return data["message"]["content"] or ""
    except (KeyError, TypeError) as e:
        raise LLMError(f"예상과 다른 응답 형식: {e}") from e


def _call_litellm(model: str, system: str | None, prompt: str,
                  temperature: float, timeout: int, max_tokens: int | None) -> str:
    """모르는 공급자용 폴백. litellm이 설치돼 있을 때만 쓴다."""
    try:
        import litellm
    except Exception as e:  # noqa: BLE001 — import 자체가 깨질 수 있다(의존성 손상)
        raise LLMError(
            f"'{model}' 은 기본 지원 공급자가 아닙니다.\n"
            "  openai / anthropic / gemini / ollama 중 하나를 쓰거나,\n"
            f"  `pip install litellm` 으로 확장하세요. (원인: {type(e).__name__})"
        ) from e

    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    kwargs = {"model": model, "messages": msgs,
              "temperature": temperature, "timeout": timeout}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    resp = litellm.completion(**kwargs)
    return resp.choices[0].message.content or ""


_DISPATCH = {
    "openai": _call_openai,
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
    "ollama": _call_ollama,
}


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------


def call_llm(
    prompt: str,
    system: str | None = None,
    model: str = "ollama/llama3.1",
    temperature: float = 0.2,
    timeout: int = 120,
    max_tokens: int | None = None,
) -> str:
    """[1]과 [7]이 공통으로 쓰는 호출 함수. 지수 백오프 재시도 포함.

    Raises:
        LLMError: 재시도를 모두 소진했거나, 재시도해도 소용없는 실패(키 없음 등).
    """
    provider, name = split_model(model)
    fn = _DISPATCH.get(provider, _call_litellm)
    target = name if provider in _DISPATCH else model

    last = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            content = fn(target, system, prompt, temperature, timeout, max_tokens)
            if not content or not content.strip():
                raise _Retryable("빈 응답")
            return content.strip()

        except LLMError:
            raise                      # 재시도해도 같은 결과 — 바로 올린다
        except _Retryable as e:
            last = str(e)
            log.warning("LLM 호출 실패 (%d/%d): %s", attempt, MAX_RETRIES, e)
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            log.warning("LLM 호출 실패 (%d/%d): %s", attempt, MAX_RETRIES, last)

        if attempt < MAX_RETRIES:
            delay = BASE_BACKOFF * (2 ** (attempt - 1))
            time.sleep(delay * (0.5 + random.random() * 0.5))

    raise LLMError(f"{MAX_RETRIES}회 재시도 소진: {last}")
