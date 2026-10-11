"""Durable blockers: things that wait for a person (an approval, a paused goal, a safe-mode notice).

A blocker is written to ``$K3CODE_HOME/blockers.db`` when it happens and stays there until a client takes it, so a
daemon restart or a detached TUI does not lose it. Channels such as Telegram or ntfy are optional and sit on top of
this table; nothing here needs them.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from k3code import sqlstore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blockers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'warning',
    text TEXT NOT NULL DEFAULT '',
    delivered_at REAL
)
"""
#: ``pending()`` reads only the rows nobody took, and a daemon that runs for months piles up the others.
_PENDING_INDEX = "CREATE INDEX IF NOT EXISTS blockers_pending ON blockers(id) WHERE delivered_at IS NULL"
#: Delivered blockers older than this are deleted when the store is opened.
RETAIN_DELIVERED_DAYS = 30


class BlockerStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlstore.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(_SCHEMA)
        self._db.execute(_PENDING_INDEX)
        self._db.commit()
        with contextlib.suppress(sqlite3.Error):  # another process holds the write lock: the next open prunes
            self.prune()

    def prune(self, max_age_days: float = RETAIN_DELIVERED_DAYS, now: float | None = None) -> int:
        """Delete delivered blockers older than ``max_age_days``; returns how many. Pending ones are never deleted."""
        cutoff = (time.time() if now is None else now) - max_age_days * 86400
        with self._db:
            cur = self._db.execute(
                "DELETE FROM blockers WHERE delivered_at IS NOT NULL AND delivered_at < ?", (cutoff,)
            )
        return cur.rowcount

    def add(self, *, session_id: str, kind: str, text: str, level: str = "warning", now: float | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO blockers (created_at, session_id, kind, level, text) VALUES (?,?,?,?,?)",
            (time.time() if now is None else now, session_id, kind, level, text[:2000]),
        )
        self._db.commit()
        return int(cur.lastrowid or 0)

    def pending(self) -> list[dict[str, Any]]:
        """Blockers no client has taken yet, oldest first."""
        rows = self._db.execute("SELECT * FROM blockers WHERE delivered_at IS NULL ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def mark_delivered(self, blocker_id: int, now: float | None = None) -> None:
        self._db.execute(
            "UPDATE blockers SET delivered_at=? WHERE id=? AND delivered_at IS NULL",
            (time.time() if now is None else now, blocker_id),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
