"""영속 조회 캐시 (SQLite).

ARS의 `~/.cache/ars/verification.db` (90일 TTL) + `/ars-cache-invalidate` 패턴 참조.

왜 필요한가: 같은 논문이 여러 쿼리에서 반복 등장한다. 검증은 결정론적이고
결과가 잘 안 바뀌므로 캐시하면 API 부하와 실행 시간이 크게 준다.

왜 TTL이 있는가: 논문 메타데이터는 고정이 아니다. 프리프린트가 정식 출판되면
DOI/venue/연도가 바뀐다. 무기한 캐시는 오래된 판정을 굳혀버린다.

**실패는 캐시하지 않는다.** `unavailable`을 캐시하면 일시적 장애가 영구
"검증 불가"로 굳는다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lookup_cache (
    cache_key   TEXT PRIMARY KEY,
    index_name  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_created ON lookup_cache(created_at);
"""


class LookupCache:
    def __init__(self, db_path: str | Path, ttl_days: int = DEFAULT_TTL_DAYS, enabled: bool = True):
        self.enabled = enabled
        self.ttl_seconds = ttl_days * 86400
        self.db_path = Path(db_path).expanduser()
        self._conn: sqlite3.Connection | None = None
        if self.enabled:
            self._connect()

    def _connect(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as e:
            # 캐시는 편의 기능이다. 못 쓰면 끄고 계속 간다.
            log.warning("캐시를 열 수 없어 비활성화합니다: %s", e)
            self.enabled = False
            self._conn = None

    def get(self, index_name: str, key: str) -> dict[str, Any] | None:
        if not self.enabled or self._conn is None:
            return None
        cache_key = f"{index_name}::{key}"
        try:
            row = self._conn.execute(
                "SELECT payload, created_at FROM lookup_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        except sqlite3.Error as e:
            log.warning("캐시 읽기 실패: %s", e)
            return None

        if row is None:
            return None
        payload, created_at = row
        if time.time() - created_at > self.ttl_seconds:
            self.delete(index_name, key)
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            # 손상된 행은 조용히 버린다 (miss-safe).
            self.delete(index_name, key)
            return None

    def set(self, index_name: str, key: str, value: dict[str, Any]) -> None:
        if not self.enabled or self._conn is None:
            return
        cache_key = f"{index_name}::{key}"
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO lookup_cache VALUES (?, ?, ?, ?)",
                (cache_key, index_name, json.dumps(value, ensure_ascii=False), time.time()),
            )
            self._conn.commit()
        except (sqlite3.Error, TypeError) as e:
            log.warning("캐시 쓰기 실패: %s", e)

    def delete(self, index_name: str, key: str) -> None:
        if not self.enabled or self._conn is None:
            return
        try:
            self._conn.execute(
                "DELETE FROM lookup_cache WHERE cache_key = ?", (f"{index_name}::{key}",)
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def invalidate_all(self) -> int:
        """`/ars-cache-invalidate` 대응. 삭제된 행 수 반환."""
        if not self.enabled or self._conn is None:
            return 0
        cur = self._conn.execute("DELETE FROM lookup_cache")
        self._conn.commit()
        return cur.rowcount

    def purge_expired(self) -> int:
        if not self.enabled or self._conn is None:
            return 0
        cutoff = time.time() - self.ttl_seconds
        cur = self._conn.execute("DELETE FROM lookup_cache WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class NullCache(LookupCache):
    """캐시 비활성 시 사용. 인터페이스만 맞춘 no-op."""

    def __init__(self):  # noqa: D107
        self.enabled = False
        self.ttl_seconds = 0
        self._conn = None
