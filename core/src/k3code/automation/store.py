"""SQLite persistence for loops, scheduled jobs, run history, automations and suggestions.

One file, ``$K3CODE_HOME/automation.db`` (WAL, so the CLI can edit while the daemon runs). Rows come back as
plain dicts; columns listed in ``JSON_COLS`` are decoded/encoded transparently.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS loops (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, prompt TEXT NOT NULL, schedule TEXT,
    self_paced INTEGER NOT NULL DEFAULT 0,
    times INTEGER, until_cond TEXT, max_ticks INTEGER NOT NULL DEFAULT 50, ticks INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active', next_run_at REAL, last_run_at REAL, created_at REAL NOT NULL,
    cwd TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', last_result TEXT NOT NULL DEFAULT '',
    stop_reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL, schedule TEXT NOT NULL, cwd TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'active', next_run_at REAL, created_at REAL NOT NULL,
    retry TEXT, quota_hold_until REAL, grace_s REAL NOT NULL DEFAULT 21600, last_status TEXT NOT NULL DEFAULT '',
    run_requested INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'auto', run_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, owner_kind TEXT NOT NULL DEFAULT 'job',
    scheduled_for REAL, started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL DEFAULT 'running',
    api_calls INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS automations (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, trigger TEXT NOT NULL, action TEXT NOT NULL,
    policy TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, last_fired_at REAL,
    fire_count INTEGER NOT NULL DEFAULT 0,
    next_run_at REAL, cwd TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS suggestions (
    id TEXT PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'catalog', spec TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL, resolved_at REAL);
"""

JSON_COLS = {"schedule", "retry", "trigger", "action", "policy", "spec"}
TABLES = {"loops", "jobs", "job_runs", "automations", "suggestions"}


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class AutomationDB:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ── generic helpers ──────────────────────────────────────────────

    @staticmethod
    def _enc(col: str, val: Any) -> Any:
        return json.dumps(val, ensure_ascii=False) if col in JSON_COLS and val is not None else val

    @staticmethod
    def _dec(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        for k in JSON_COLS & out.keys():
            if isinstance(out[k], str):
                out[k] = json.loads(out[k])
        return out

    def insert(self, table: str, **fields: Any) -> int:
        assert table in TABLES
        cols = list(fields)
        cur = self._db.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",  # noqa: S608
            [self._enc(c, fields[c]) for c in cols],
        )
        self._db.commit()
        return cur.lastrowid or 0

    def update(self, table: str, row_id: str | int, **fields: Any) -> None:
        assert table in TABLES
        if not fields:
            return
        sets = ",".join(f"{c}=?" for c in fields)
        self._db.execute(
            f"UPDATE {table} SET {sets} WHERE id=?",  # noqa: S608
            [*(self._enc(c, v) for c, v in fields.items()), row_id],
        )
        self._db.commit()

    def get(self, table: str, row_id: str | int) -> dict[str, Any] | None:
        assert table in TABLES
        return self._dec(self._db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())  # noqa: S608

    def find(self, table: str, prefix: str) -> dict[str, Any] | None:
        """Row by exact id or unique id prefix."""
        row = self.get(table, prefix)
        if row is not None:
            return row
        rows = self._db.execute(f"SELECT * FROM {table} WHERE id LIKE ?", (prefix + "%",)).fetchall()  # noqa: S608
        return self._dec(rows[0]) if len(rows) == 1 else None

    def rows(
        self, table: str, where: str = "", args: tuple[Any, ...] = (), order: str = "rowid", limit: int = 0
    ) -> list[dict[str, Any]]:
        assert table in TABLES
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "") + f" ORDER BY {order}"  # noqa: S608
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._dec(r) for r in self._db.execute(sql, args).fetchall()]  # type: ignore[misc]

    def delete(self, table: str, row_id: str | int) -> bool:
        assert table in TABLES
        cur = self._db.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))  # noqa: S608
        self._db.commit()
        return cur.rowcount > 0

    def runs(self, owner: str, limit: int = 5) -> list[dict[str, Any]]:
        return self.rows("job_runs", "owner=?", (owner,), order="id DESC", limit=limit)

    def close(self) -> None:
        self._db.close()
