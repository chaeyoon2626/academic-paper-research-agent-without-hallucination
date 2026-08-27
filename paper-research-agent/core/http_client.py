"""API 클라이언트 공통 레이어: 레이트 리밋, 재시도, 장애 래치.

ARS의 `semantic_scholar_client.py` / `openalex_client.py` / `crossref_client.py`에서
가져온 세 가지 설계 판단:

1. **스로틀은 `time.monotonic`으로 잰다.** `time.time`은 NTP 동기화로 뒤로 갈 수
   있어서 sleep 계산이 음수가 되거나 폭주한다. (ARS가 v3.9.3에서 실제로 고친 버그)

2. **장애 래치(outage latch).** 네트워크 레벨 실패가 나면 그 인덱스를 세션 동안
   "죽은 것"으로 표시하고 더 안 두드린다. 이게 중요한 이유는 성능이 아니라
   **판정 정확도**다 — 인덱스가 죽어서 못 찾은 걸 "논문이 없다"로 처리하면
   멀쩡한 논문이 전부 가짜로 찍힌다. 래치가 켜지면 호출부는 `unavailable`을
   받고, 검증기는 그걸 `not_found`가 아니라 `uncertain`으로 흡수한다.

3. **재시도는 5xx/429/네트워크 오류에만.** 404는 재시도해도 404다. 오히려
   404는 "명시적으로 없음"이라는 귀중한 신호라서 그대로 올려보내야 한다.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass

import requests

from core.text_similarity import BACKOFF_SECONDS, MAX_RETRIES

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class IndexUnavailable(Exception):
    """인덱스에 도달하지 못했다. '논문이 없다'는 뜻이 절대 아니다."""


class Aborted(Exception):
    """사용자가 중단을 요청했다. 대기 도중에도 즉시 빠져나온다."""


def interruptible_sleep(seconds: float, abort: threading.Event | None) -> None:
    """중단 신호가 오면 즉시 깨는 sleep.

    `time.sleep(3)`은 중간에 못 깬다. arXiv는 예의상 3초를 쉬고, 재시도
    백오프는 최대 30초까지 간다. 그 사이 사용자가 중단을 눌러도 반응이
    없으면 "먹통"으로 느껴진다. Event.wait()은 신호가 오면 바로 돌아온다.
    """
    if seconds <= 0:
        return
    if abort is None:
        time.sleep(seconds)
        return
    if abort.wait(seconds):
        raise Aborted("사용자 중단")


@dataclass
class Response:
    status_code: int
    json_body: dict | list | None = None
    text_body: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_absent(self) -> bool:
        """식별자가 인덱스에 없다고 명시적으로 응답한 경우."""
        return self.status_code in (404, 410)


class ThrottledClient:
    """인덱스 하나당 인스턴스 하나. 스로틀과 래치를 인스턴스가 소유한다."""

    def __init__(
        self,
        name: str,
        min_interval: float = 1.0,
        timeout: int = 20,
        user_agent: str = "paper-research-agent/0.1",
        mailto: str | None = None,
        abort: threading.Event | None = None,
    ):
        self.name = name
        # 중단 신호. 모든 클라이언트가 같은 Event를 공유한다.
        self.abort = abort
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_call: float = 0.0
        self._outage = False
        # 여러 후보를 동시에 검증하면 같은 클라이언트를 여러 스레드가 쓴다.
        # 락이 없으면 두 스레드가 동시에 "내 차례"라고 판단해 레이트 리밋을 넘긴다.
        self._turn_lock = threading.Lock()
        self._session = requests.Session()

        ua = user_agent
        # OpenAlex/Crossref는 연락처를 UA에 넣으면 polite pool로 라우팅해준다.
        if mailto:
            ua = f"{user_agent} (mailto:{mailto})"
        self._session.headers.update({"User-Agent": ua, "Accept": "application/json"})

    # -- 래치 -----------------------------------------------------------------

    @property
    def in_outage(self) -> bool:
        return self._outage

    def reset_outage_latch(self) -> None:
        """장시간 실행되는 배치에서 주기적으로 풀어주기 위한 탈출구."""
        self._outage = False

    # -- 스로틀 ---------------------------------------------------------------

    def _wait_turn(self) -> None:
        """내 차례가 될 때까지 대기. 스레드 안전.

        슬롯을 락 안에서 '예약'하고 sleep은 락 밖에서 한다. 락을 쥔 채 자면
        다른 스레드가 예약조차 못 해서 병렬 처리가 무의미해진다.
        """
        with self._turn_lock:
            now = time.monotonic()      # time.time() 아님 — 위 주석 2번 참조
            slot = max(now, self._last_call + self.min_interval)
            self._last_call = slot
        delay = slot - time.monotonic()
        if delay > 0:
            interruptible_sleep(delay, self.abort)

    # -- 요청 -----------------------------------------------------------------

    def get(
        self,
        url: str,
        params: dict | None = None,
        accept: str | None = None,
        headers: dict | None = None,
    ) -> Response:
        """GET 1회. 재시도 포함.

        Returns:
            Response — 2xx 또는 4xx(404 포함). 4xx는 유의미한 정보다.
        Raises:
            IndexUnavailable — 재시도 소진, 네트워크 오류, 또는 래치가 이미 켜짐.
        """
        if self.abort is not None and self.abort.is_set():
            raise Aborted("사용자 중단")
        if self._outage:
            raise IndexUnavailable(f"{self.name}: 이번 세션에서 이미 장애로 표시됨")

        # CORE 같은 곳은 Bearer 인증이 필요하다. accept는 편의용 단축 인자.
        req_headers: dict = dict(headers) if headers else {}
        if accept:
            req_headers["Accept"] = accept
        last_err = ""

        for attempt in range(1, MAX_RETRIES + 1):
            self._wait_turn()
            try:
                r = self._session.get(
                    url, params=params, timeout=self.timeout,
                    headers=req_headers or None,
                )
            except requests.RequestException as e:
                last_err = f"network: {e}"
                log.warning("%s 네트워크 오류 (%d/%d): %s", self.name, attempt, MAX_RETRIES, e)
                self._sleep_backoff(attempt)
                continue

            if r.status_code in RETRYABLE_STATUS:
                last_err = f"http {r.status_code}"
                log.warning("%s %s (%d/%d)", self.name, last_err, attempt, MAX_RETRIES)
                self._sleep_backoff(attempt, retry_after=r.headers.get("Retry-After"))
                continue

            # 2xx 또는 재시도 불가 4xx → 그대로 반환
            body: dict | list | None = None
            text = ""
            try:
                if "json" in r.headers.get("Content-Type", ""):
                    body = r.json()
                else:
                    text = r.text
            except ValueError as e:
                # 200인데 JSON 파싱 실패 = 인덱스가 이상한 걸 준 것.
                # ARS가 #129에서 한 것처럼 Unavailable로 감싼다. 성공 처리하면 안 됨.
                self._outage = True
                raise IndexUnavailable(f"{self.name}: 응답 파싱 실패: {e}") from e

            return Response(status_code=r.status_code, json_body=body, text_body=text)

        self._outage = True
        raise IndexUnavailable(f"{self.name}: {MAX_RETRIES}회 재시도 소진 ({last_err})")

    def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                interruptible_sleep(min(float(retry_after), 30.0), self.abort)
                return
            except (TypeError, ValueError):
                pass
        # 지수 백오프 + 지터
        delay = BACKOFF_SECONDS * (2 ** (attempt - 1))
        interruptible_sleep(min(delay, 30.0) * (0.5 + random.random() * 0.5), self.abort)
