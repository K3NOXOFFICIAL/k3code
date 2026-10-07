"""SQLite-backed session persistence.

One row per session at ``$K3CODE_HOME/sessions.db`` (table ``sessions``):
``session_id`` primary key, title, model, provider, cwd, message list
(JSON array of ``{role, content, tool_calls?, tool_call_id?, name?}``),
usage JSON, timestamps. Live sessions (AgentLoop state, asyncio tasks) are
kept in memory by the server; this store only persists what resume needs.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StoredSession:
    session_id: str
    title: str = ""
    model: str = ""
    provider: str = ""
    cwd: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)  # per-session extras: add_dirs, mode


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    messages TEXT NOT NULL DEFAULT '[]',
    usage TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
)
"""


class SessionStore:
    """Tiny sqlite session store. One connection, used from the event loop thread."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.execute(_SCHEMA)
        with contextlib.suppress(sqlite3.OperationalError):  # column already exists
            self._db.execute("ALTER TABLE sessions ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
        self._db.commit()

    def create(self, *, title: str = "", model: str = "", provider: str = "", cwd: str = "") -> StoredSession:
        now = time.time()
        sess = StoredSession(
            session_id=uuid.uuid4().hex[:16],
            title=title,
            model=model,
            provider=provider,
            cwd=cwd,
            created_at=now,
            updated_at=now,
        )
        self._db.execute(
            "INSERT INTO sessions (session_id, title, model, provider, cwd, messages, usage, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (sess.session_id, sess.title, sess.model, sess.provider, sess.cwd, "[]", "{}", now, now),
        )
        self._db.commit()
        return sess

    def get(self, session_id: str) -> StoredSession | None:
        row = self._db.execute(
            "SELECT session_id, title, model, provider, cwd, messages, usage, created_at, updated_at, meta"
            " FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredSession(
            session_id=row[0],
            title=row[1] or "",
            model=row[2] or "",
            provider=row[3] or "",
            cwd=row[4] or "",
            messages=json.loads(row[5] or "[]"),
            usage=json.loads(row[6] or "{}"),
            created_at=row[7],
            updated_at=row[8],
            meta=json.loads(row[9] or "{}"),
        )

    def list(self, *, limit: int = 50) -> list[StoredSession]:
        rows = self._db.execute(
            "SELECT session_id, title, model, provider, cwd, messages, usage, created_at, updated_at"
            " FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        out: list[StoredSession] = []
        for row in rows:
            messages = json.loads(row[5] or "[]")
            out.append(
                StoredSession(
                    session_id=row[0],
                    title=row[1] or "",
                    model=row[2] or "",
                    provider=row[3] or "",
                    cwd=row[4] or "",
                    messages=messages,
                    usage=json.loads(row[6] or "{}"),
                    created_at=row[7],
                    updated_at=row[8],
                )
            )
        return out

    def save(self, sess: StoredSession) -> None:
        sess.updated_at = time.time()
        self._db.execute(
            "UPDATE sessions SET title=?, model=?, provider=?, cwd=?, messages=?, usage=?, updated_at=?, meta=?"
            " WHERE session_id=?",
            (
                sess.title,
                sess.model,
                sess.provider,
                sess.cwd,
                json.dumps(sess.messages, ensure_ascii=False),
                json.dumps(sess.usage, ensure_ascii=False),
                sess.updated_at,
                json.dumps(sess.meta, ensure_ascii=False),
                sess.session_id,
            ),
        )
        self._db.commit()

    def delete(self, session_id: str) -> bool:
        cur = self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self._db.commit()
        return cur.rowcount > 0

    def most_recent(self) -> StoredSession | None:
        rows = self.list(limit=1)
        return rows[0] if rows else None

    def close(self) -> None:
        self._db.close()
