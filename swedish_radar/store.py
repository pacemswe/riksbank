"""Persists discovery runs so the web UI and later runs can read them back."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import RunResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    criteria    TEXT NOT NULL,
    match_count INTEGER NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_releases (
    release_id  TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    last_run_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_created ON runs (created_at DESC);
"""


class Store:
    """A thin SQLite wrapper - runs are stored whole as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if path != Path(":memory:"):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save_run(self, result: RunResult, *, today: Optional[date] = None) -> int:
        payload = result.to_dict(today)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO runs (created_at, criteria, match_count, payload) VALUES (?, ?, ?, ?)",
                (now, ",".join(result.criteria), len(result.matches), json.dumps(payload, ensure_ascii=False)),
            )
            run_id = int(cursor.lastrowid or 0)
            self._conn.executemany(
                "INSERT INTO seen_releases (release_id, first_seen, last_run_id) VALUES (?, ?, ?) "
                "ON CONFLICT(release_id) DO UPDATE SET last_run_id = excluded.last_run_id",
                [(m.release.id, now, run_id) for m in result.matches],
            )
            self._conn.commit()
        return run_id

    def latest_run(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, created_at, criteria, match_count, payload FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _row_to_run(row) if row else None

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, created_at, criteria, match_count, payload FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _row_to_run(row) if row else None

    def list_runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, criteria, match_count FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "criteria": row["criteria"].split(",") if row["criteria"] else [],
                "match_count": row["match_count"],
            }
            for row in rows
        ]

    def first_seen(self, release_ids: List[str]) -> Dict[str, str]:
        """When each release first appeared in any stored run - powers "new since last run"."""
        if not release_ids:
            return {}
        placeholders = ",".join("?" for _ in release_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT release_id, first_seen FROM seen_releases WHERE release_id IN ({placeholders})",
                release_ids,
            ).fetchall()
        return {row["release_id"]: row["first_seen"] for row in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _row_to_run(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        payload = {"matches": [], "count": 0}
    payload["run_id"] = row["id"]
    payload["stored_at"] = row["created_at"]
    return payload
