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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code import sqlstore

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


@dataclass
class SessionSummary:
    """A session without its transcript: what a listing shows, read from columns, never by parsing ``messages``."""

    session_id: str
    title: str = ""
    model: str = ""
    provider: str = ""
    cwd: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    message_count: int = 0
    preview: str = ""


PREVIEW_CHARS = 120


def dump_messages(messages: list[dict[str, Any]]) -> str:
    """``json.dumps(messages, ensure_ascii=False)``, one message per encoder call. The C encoder keeps the GIL for a
    whole call, so a transcript encoded in one piece freezes every other thread; in pieces they run in between."""
    return "[" + ", ".join(json.dumps(m, ensure_ascii=False) for m in messages) + "]"


def summarize_messages(messages: list[dict[str, Any]]) -> tuple[int, str]:
    """``(message count, preview)``: the preview is the first non-empty user message, cut to ``PREVIEW_CHARS``."""
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            return len(messages), str(m["content"])[:PREVIEW_CHARS]
    return len(messages), ""


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
        self._executor: ThreadPoolExecutor | None = None
        self._writer: SessionStore | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlstore.connect(self.path)
        self._db.execute(_SCHEMA)
        with contextlib.suppress(sqlite3.OperationalError):  # column already exists
            self._db.execute("ALTER TABLE sessions ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
        # -1 = not computed yet (a row written before these columns existed): the first listing fills it in
        with contextlib.suppress(sqlite3.OperationalError):
            self._db.execute("ALTER TABLE sessions ADD COLUMN message_count INTEGER NOT NULL DEFAULT -1")
        with contextlib.suppress(sqlite3.OperationalError):
            self._db.execute("ALTER TABLE sessions ADD COLUMN preview TEXT NOT NULL DEFAULT ''")
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
            "INSERT INTO sessions"
            " (session_id, title, model, provider, cwd, messages, usage, created_at, updated_at, message_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,0)",
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
        count, preview = summarize_messages(sess.messages)
        self._db.execute(
            "INSERT INTO sessions"
            " (session_id, title, model, provider, cwd, messages, usage, created_at, updated_at, meta,"
            " message_count, preview)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sess.session_id,
                sess.title,
                sess.model,
                sess.provider,
                sess.cwd,
                dump_messages(sess.messages),
                json.dumps(sess.usage, ensure_ascii=False),
                sess.created_at,
                sess.updated_at,
                json.dumps(sess.meta, ensure_ascii=False),
                count,
                preview,
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

    _SUMMARY_COLUMNS = "session_id, title, model, provider, cwd, created_at, updated_at, message_count, preview"

    def list(self, *, limit: int = 50, include_automation: bool = True, offset: int = 0) -> list[SessionSummary]:
        """The newest sessions as summaries. Reads columns only: a transcript can be huge and is never parsed."""
        where = "" if include_automation else " WHERE COALESCE(json_extract(meta, '$.origin'), '') != 'automation'"
        rows = self._db.execute(
            f"SELECT {self._SUMMARY_COLUMNS} FROM sessions{where}"  # noqa: S608
            " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (max(1, limit), max(0, offset)),
        ).fetchall()
        return [self._summary(row) for row in rows]

    def summaries(self, ids: list[str]) -> list[SessionSummary]:
        """Summaries of ``ids`` in that order; an id with no row is skipped."""
        out: list[SessionSummary] = []
        for sid in ids:
            row = self._db.execute(
                f"SELECT {self._SUMMARY_COLUMNS} FROM sessions WHERE session_id = ?",  # noqa: S608
                (sid,),
            ).fetchone()
            if row is not None:
                out.append(self._summary(row))
        return out

    def _summary(self, row: tuple[Any, ...]) -> SessionSummary:
        count, preview = row[7], row[8] or ""
        if count < 0:  # written before the columns existed: parse this one transcript once and keep the answer
            count, preview = self._backfill_summary(row[0])
        return SessionSummary(
            session_id=row[0],
            title=row[1] or "",
            model=row[2] or "",
            provider=row[3] or "",
            cwd=row[4] or "",
            created_at=row[5],
            updated_at=row[6],
            message_count=count,
            preview=preview,
        )

    def _backfill_summary(self, session_id: str) -> tuple[int, str]:
        row = self._db.execute("SELECT messages FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return 0, ""
        count, preview = summarize_messages(json.loads(row[0] or "[]"))
        with self._db:  # `message_count < 0`: a save that landed since the read has the real values already
            self._db.execute(
                "UPDATE sessions SET message_count=?, preview=? WHERE session_id=? AND message_count < 0",
                (count, preview, session_id),
            )
        return count, preview

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
        count, preview = summarize_messages(sess.messages)
        messages = dump_messages(sess.messages)
        with self._db:
            cur = self._db.execute(
                "UPDATE sessions SET title=?, model=?, provider=?, cwd=?, messages=?, usage=?, updated_at=?, meta=?,"
                " message_count=?, preview=? WHERE session_id=?",
                (
                    sess.title,
                    sess.model,
                    sess.provider,
                    sess.cwd,
                    messages,
                    json.dumps(sess.usage, ensure_ascii=False),
                    sess.updated_at,
                    json.dumps(sess.meta, ensure_ascii=False),
                    count,
                    preview,
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

    def checkpoint(self, session_id: str, messages: list[dict[str, Any]], model: str = "") -> None:
        """Write only the transcript (and the model, when the row has none): a mid-turn checkpoint must not carry the
        title or meta it was snapshotted with over a newer value saved in between. A missing row stays missing."""
        count, preview = summarize_messages(messages)
        payload = dump_messages(messages)
        with self._db:
            self._db.execute(
                "UPDATE sessions SET messages=?, model=CASE WHEN model='' THEN ? ELSE model END, updated_at=?,"
                " message_count=?, preview=? WHERE session_id=?",
                (payload, model, time.time(), count, preview, session_id),
            )

    async def _on_writer(self, name: str, *args: Any) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="k3code-session-writer")
        await asyncio.get_running_loop().run_in_executor(self._executor, self._call_writer, name, *args)

    def _call_writer(self, name: str, *args: Any) -> None:
        if self._writer is None:  # a sqlite connection may only be used by the thread that made it
            self._writer = SessionStore(self.path)
        getattr(self._writer, name)(*args)

    async def save_async(self, sess: StoredSession) -> None:
        """:meth:`save` on a worker thread with its own connection, so a large transcript does not stall the event
        loop. One worker, so writes land in the order they were queued. Pass a snapshot, not a live object."""
        await self._on_writer("save", sess)

    async def checkpoint_async(self, session_id: str, messages: list[dict[str, Any]], model: str = "") -> None:
        """:meth:`checkpoint` on the same worker (so after any queued write, before any later one)."""
        await self._on_writer("checkpoint", session_id, messages, model)

    def _close_writer(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None

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
        swept: list[str] | None = None,
    ) -> int:
        """Delete stored sessions nobody can come back to; returns how many went.

        Only rows that are all of: no message, no title, no meta key at all (mode, add_dirs, reasoning effort, goal,
        origin, background, branches... each is something the user or an automation set), last saved more than
        ``max_age`` seconds before ``now``, and not ``keep(session_id)`` (the caller's live sessions and those an
        loop or automation points at). ``model`` is no signal: every new session gets the default model.
        A workspace move leaves no marker either; the age limit covers it (moving saves the row).

        Each deleted row leaves a tombstone in ``swept_sessions`` (same transaction), so a process that still holds
        the session restores it on its next :meth:`save`; tombstones older than ``SWEPT_TOMBSTONE_MAX_AGE_S`` go here.
        The ids of the deleted rows are appended to ``swept`` when given (the caller removes their journals).
        """
        now = self._prune_tombstones(now)
        ids = self._old_ids(now, max_age)
        return sum(
            self._sweep_batch(ids[i : i + SWEEP_BATCH], keep, now, max_age, swept)
            for i in range(0, len(ids), SWEEP_BATCH)
        )

    async def sweep_empty_async(
        self,
        *,
        now: float | None = None,
        max_age: float = EMPTY_SESSION_MAX_AGE_S,
        keep: Callable[[str], bool] = lambda _sid: False,
        swept: list[str] | None = None,
    ) -> int:
        """:meth:`sweep_empty` in small batches that yield to the event loop between them (the connection is not
        thread-safe, so no thread). Each batch re-reads its rows, so a session that gained a message meanwhile stays."""
        now = self._prune_tombstones(now)
        ids = self._old_ids(now, max_age)
        deleted = 0
        for i in range(0, len(ids), SWEEP_BATCH):
            deleted += self._sweep_batch(ids[i : i + SWEEP_BATCH], keep, now, max_age, swept)
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

    def _sweep_batch(
        self, ids: list[str], keep: Callable[[str], bool], now: float, max_age: float, swept: list[str] | None
    ) -> int:
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
                if swept is not None:
                    swept.append(sid)
        return deleted

    def most_recent(self) -> StoredSession | None:
        """The session to continue: the newest one the user worked in (cron/loop/automation runs and sessions with no
        message are not that: resuming one opens an empty chat)."""
        rows = self.worked_in(limit=1)
        return self.get(rows[0][0]) if rows else None

    def compact(self) -> int:
        """Return the pages of deleted transcripts to the filesystem; bytes freed (see :func:`sqlstore.compact`)."""
        return sqlstore.compact(self._db)

    def close(self) -> None:
        if self._executor is not None:  # lets every queued save finish first
            self._executor.submit(self._close_writer).result()
            self._executor.shutdown(wait=True)
            self._executor = None
        self._db.close()
