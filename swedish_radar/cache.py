"""SQLite-backed response cache.

Spotify and MusicBrainz data barely changes between runs, and MusicBrainz asks
for one request per second.  Caching is what makes repeated runs (and the web
UI) usable at all.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    stored_at  REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS http_cache_expires ON http_cache (expires_at);
"""


class Cache:
    """A TTL key/value store.  ``enabled=False`` turns every call into a no-op."""

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self._clock = clock
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self.hits = 0
        self.misses = 0
        if enabled:
            self._connect()

    def _connect(self) -> None:
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, key: str) -> Optional[object]:
        if not self.enabled or self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, expires_at FROM http_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            self.misses += 1
            return None
        payload, expires_at = row
        if expires_at < self._clock():
            self.misses += 1
            self.delete(key)
            return None
        self.hits += 1
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            self.delete(key)
            return None

    def set(self, key: str, value: object, ttl: int) -> None:
        if not self.enabled or self._conn is None:
            return
        now = self._clock()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO http_cache (key, payload, stored_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (key, json.dumps(value), now, now + ttl),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        if not self.enabled or self._conn is None:
            return
        with self._lock:
            self._conn.execute("DELETE FROM http_cache WHERE key = ?", (key,))
            self._conn.commit()

    def purge_expired(self) -> int:
        if not self.enabled or self._conn is None:
            return 0
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM http_cache WHERE expires_at < ?", (self._clock(),)
            )
            self._conn.commit()
            return cursor.rowcount or 0

    def clear(self) -> None:
        if not self.enabled or self._conn is None:
            return
        with self._lock:
            self._conn.execute("DELETE FROM http_cache")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
