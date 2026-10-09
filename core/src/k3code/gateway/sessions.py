"""SQLite-backed session persistence.

One row per session at ``$K3CODE_HOME/sessions.db`` (table ``sessions``):
``session_id`` primary key, title, model, provider, cwd, message list
(JSON array of ``{role, content, tool_calls?, tool_call_id?, name?}``),
usage JSON, timestamps. Live sessions (AgentLoop state, asyncio tasks) are
kept in memory by the server; this store only persists what resume needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The daemon start sweep deletes empty, unnamed stored sessions untouched this long (see SessionStore.sweep_empty).
EMPTY_SESSION_MAX_AGE_S = 30 * 24 * 3600.0
#: The sweep forgets its tombstone of a deleted row after this long (a later save no longer restores it).
SWEPT_TOMBSTONE_MAX_AGE_S = 90 * 24 * 3600.0
SWEEP_BATCH = 200


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
        self._db.execute("CREATE INDEX IF NOT EXISTS sessions_updated_at ON sessions (updated_at)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS swept_sessions (session_id TEXT PRIMARY KEY, swept_at REAL NOT NULL)"
        )
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

    def insert(self, sess: StoredSession) -> StoredSession:
        """Insert a fully-formed session (import/fork); the caller guarantees the id is free."""
        now = time.time()
        sess.created_at = sess.created_at or now
        sess.updated_at = now
        self._insert_row(sess)
        self._db.commit()
        return sess

    def _insert_row(self, sess: StoredSession) -> None:
        """The full-row INSERT shared by :meth:`insert` and a save that restores a swept row; no commit."""
        self._db.execute(
            "INSERT INTO sessions"
            " (session_id, title, model, provider, cwd, messages, usage, created_at, updated_at, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sess.session_id,
                sess.title,
                sess.model,
                sess.provider,
                sess.cwd,
                json.dumps(sess.messages, ensure_ascii=False),
                json.dumps(sess.usage, ensure_ascii=False),
                sess.created_at,
                sess.updated_at,
                json.dumps(sess.meta, ensure_ascii=False),
            ),
        )

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:16]

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

    def with_goal_status(self, *statuses: str) -> list[StoredSession]:
        """Full sessions whose goal is in one of ``statuses`` (boot resume and the watchdog read only these)."""
        if not statuses:
            return []
        marks = ",".join("?" * len(statuses))
        ids = self._db.execute(
            f"SELECT session_id FROM sessions WHERE json_extract(meta, '$.goal.status') IN ({marks})",  # noqa: S608
            statuses,
        ).fetchall()
        return [s for s in (self.get(row[0]) for row in ids) if s is not None]

    def list(self, *, limit: int = 50, include_automation: bool = True, offset: int = 0) -> list[StoredSession]:
        where = "" if include_automation else " WHERE COALESCE(json_extract(meta, '$.origin'), '') != 'automation'"
        rows = self._db.execute(
            "SELECT session_id, title, model, provider, cwd, messages, usage, created_at, updated_at"
            f" FROM sessions{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",  # noqa: S608
            (max(1, limit), max(0, offset)),
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

    def worked_in(self, *, limit: int) -> list[tuple[str, str]]:
        """``(session_id, cwd)`` of the newest ``limit`` sessions with at least one message and no automation origin.

        Decided in SQL, so no transcript is parsed or copied into Python: every write path stores an empty
        transcript as exactly ``'[]'``.
        """
        return self._db.execute(
            "SELECT session_id, cwd FROM sessions"
            " WHERE messages NOT IN ('', '[]') AND COALESCE(json_extract(meta, '$.origin'), '') != 'automation'"
            " ORDER BY updated_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()

    def save(self, sess: StoredSession) -> None:
        """Update the row; a missing row stays missing (deleted sessions are not resurrected), unless the start
        sweep removed it: then its tombstone is there and the row is restored from ``sess`` (another process, such
        as a standalone stdio TUI, may hold the session open past the sweep's age limit)."""
        sess.updated_at = time.time()
        with self._db:
            cur = self._db.execute(
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
            if cur.rowcount:
                return
            gone = self._db.execute("DELETE FROM swept_sessions WHERE session_id = ?", (sess.session_id,))
            if not gone.rowcount:
                return
            sess.created_at = sess.created_at or sess.updated_at
            self._insert_row(sess)
        logger.info("restored stored session %s removed by the empty-session sweep", sess.session_id)

    def delete(self, session_id: str) -> bool:
        """Delete the row and any sweep tombstone for it, so no later save brings the session back."""
        with self._db:
            cur = self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            self._db.execute("DELETE FROM swept_sessions WHERE session_id = ?", (session_id,))
        return cur.rowcount > 0

    def sweep_empty(
        self,
        *,
        now: float | None = None,
        max_age: float = EMPTY_SESSION_MAX_AGE_S,
        keep: Callable[[str], bool] = lambda _sid: False,
    ) -> int:
        """Delete stored sessions nobody can come back to; returns how many went.

        Only rows that are all of: no message, no title, no meta key at all (mode, add_dirs, reasoning effort, goal,
        origin, background, branches... each is something the user or an automation set), last saved more than
        ``max_age`` seconds before ``now``, and not ``keep(session_id)`` (the caller's live sessions and those an
        loop or automation points at). ``model`` is no signal: every new session gets the default model.
        A workspace move leaves no marker either; the age limit covers it (moving saves the row).

        Each deleted row leaves a tombstone in ``swept_sessions`` (same transaction), so a process that still holds
        the session restores it on its next :meth:`save`; tombstones older than ``SWEPT_TOMBSTONE_MAX_AGE_S`` go here.
        """
        now = self._prune_tombstones(now)
        ids = self._old_ids(now, max_age)
        return sum(
            self._sweep_batch(ids[i : i + SWEEP_BATCH], keep, now, max_age) for i in range(0, len(ids), SWEEP_BATCH)
        )

    async def sweep_empty_async(
        self,
        *,
        now: float | None = None,
        max_age: float = EMPTY_SESSION_MAX_AGE_S,
        keep: Callable[[str], bool] = lambda _sid: False,
    ) -> int:
        """:meth:`sweep_empty` in small batches that yield to the event loop between them (the connection is not
        thread-safe, so no thread). Each batch re-reads its rows, so a session that gained a message meanwhile stays."""
        now = self._prune_tombstones(now)
        ids = self._old_ids(now, max_age)
        deleted = 0
        for i in range(0, len(ids), SWEEP_BATCH):
            deleted += self._sweep_batch(ids[i : i + SWEEP_BATCH], keep, now, max_age)
            await asyncio.sleep(0)
        return deleted

    def _prune_tombstones(self, now: float | None) -> float:
        """Forget tombstones older than ``SWEPT_TOMBSTONE_MAX_AGE_S``; returns the sweep's clock."""
        now = time.time() if now is None else now
        with self._db:
            self._db.execute("DELETE FROM swept_sessions WHERE swept_at < ?", (now - SWEPT_TOMBSTONE_MAX_AGE_S,))
        return now

    def _old_ids(self, now: float, max_age: float) -> list[str]:
        """Ids only, straight off the ``updated_at`` index: no transcript is read here."""
        cutoff = now - max_age
        return [r[0] for r in self._db.execute("SELECT session_id FROM sessions WHERE updated_at < ?", (cutoff,))]

    def _sweep_batch(self, ids: list[str], keep: Callable[[str], bool], now: float, max_age: float) -> int:
        marks = ",".join("?" * len(ids))
        rows = self._db.execute(
            f"SELECT session_id, title, meta FROM sessions WHERE session_id IN ({marks}) AND messages IN ('', '[]')",  # noqa: S608
            ids,
        ).fetchall()
        doomed = [sid for sid, title, meta in rows if not title and meta in ("", "{}") and not keep(sid)]
        deleted = 0
        with self._db:  # a row and its tombstone go together or not at all
            for sid in doomed:
                # the read above ran outside this transaction: another process may have saved the row since, so the
                # DELETE repeats every condition of the read and the guard (same shape, in SQL) and may match nothing
                cur = self._db.execute(
                    "DELETE FROM sessions WHERE session_id = ? AND messages IN ('', '[]') AND updated_at < ?"
                    " AND (title IS NULL OR title = '') AND meta IN ('', '{}')",
                    (sid, now - max_age),
                )
                if cur.rowcount != 1:
                    logger.info("empty-session sweep left %s alone: it changed or went while the sweep ran", sid)
                    continue
                self._db.execute(
                    "INSERT OR REPLACE INTO swept_sessions (session_id, swept_at) VALUES (?, ?)", (sid, now)
                )
                deleted += 1
        return deleted

    def most_recent(self) -> StoredSession | None:
        """The session to continue: the newest one the user worked in (cron/loop/automation runs and sessions with no
        message are not that: resuming one opens an empty chat)."""
        rows = self.worked_in(limit=1)
        return self.get(rows[0][0]) if rows else None

    def close(self) -> None:
        self._db.close()
