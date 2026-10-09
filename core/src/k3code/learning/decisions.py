"""General decision log: ``$K3CODE_HOME/learning/decisions.db`` (SQLite), migrating ``decisions.jsonl``."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code import sqlstore
from k3code.paths import ensure_private_dir
from k3code.redact import scrub_text

KINDS = (
    "approval",
    "model_switch",
    "proposal",
    "plan",
    "interrupt",
    "undo",
    "scope",
    "config",
    "auto_apply",
    "tool_error",  # subject = normalised error signature, choice = error class, detail.tool (see k3code.toolerrors)
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    choice TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS decisions_kind ON decisions(kind, project);
"""


def scrub(text: str) -> str:
    """Strip anything that looks like a credential before it reaches the log: k3code.redact.scrub_text.

    Kept as a name for older imports; its own regex missed URL userinfo, ``--password x``, PEM blocks,
    ``github_pat_``, ``xoxb-`` and JWTs, and its output reached USER.md, project notes, mem0 and skill drafts.
    """
    return scrub_text(text)


def _scrub_obj(o: Any) -> Any:
    if isinstance(o, str):
        return scrub_text(o)
    if isinstance(o, dict):
        return {k: _scrub_obj(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_scrub_obj(v) for v in o]
    return o


def project_id(cwd: str | Path) -> str:
    """Stable id for a project: the git remote URL when there is one, else a hash of the path."""
    p = Path(cwd)
    try:
        out = subprocess.run(
            ["git", "-C", str(p), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        url = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        url = ""
    if url:
        url = re.sub(r"^[a-z+]+://([^@/]+@)?", "", url)  # drop scheme + userinfo (may hold a token)
        return "git:" + url.removesuffix(".git")
    return "path:" + hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:12]


#: Decisions older than this are deleted by :meth:`DecisionLog.prune` (``retention.decisions_days``).
MAX_AGE_DAYS = 365


class DecisionLog:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.home = Path(home)
        self.path = self.home / "learning" / "decisions.db"
        ensure_private_dir(self.path.parent)
        self.clock = clock
        self._db = sqlstore.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._migrate_actor()
        self._db.commit()
        self._pid_cache: dict[str, str] = {}
        self.migrate_jsonl()

    def _migrate_actor(self) -> None:
        """``actor`` as a column (it lived only in the detail JSON, so every query loaded every row and filtered in
        Python, once per turn end), indexed with kind and time."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(decisions)")}
        if "actor" not in cols:
            self._db.execute("ALTER TABLE decisions ADD COLUMN actor TEXT NOT NULL DEFAULT 'user'")
            self._db.execute(
                "UPDATE decisions SET actor = COALESCE(json_extract(detail, '$.actor'), 'user')"
                " WHERE json_valid(detail)"
            )
        self._db.execute("CREATE INDEX IF NOT EXISTS decisions_kind_actor_ts ON decisions(kind, actor, ts)")

    def prune(self, max_age_days: float = MAX_AGE_DAYS) -> int:
        """Delete decisions older than ``max_age_days`` (the daemon calls it at start); returns rows deleted."""
        with self._db:
            cur = self._db.execute("DELETE FROM decisions WHERE ts < ?", (self.clock() - max_age_days * 86400,))
        return cur.rowcount

    def close(self) -> None:
        self._db.close()

    def _project(self, cwd: str) -> str:
        if not cwd:
            return ""
        if cwd not in self._pid_cache:
            self._pid_cache[cwd] = project_id(cwd)
        return self._pid_cache[cwd]

    def record(
        self,
        kind: str,
        *,
        session: str = "",
        cwd: str = "",
        subject: str = "",
        choice: str = "",
        detail: dict[str, Any] | None = None,
        ts: float | None = None,
        project: str | None = None,
        actor: str = "user",
    ) -> int:
        """``actor``: ``user`` (a human decided) or ``auto`` (unattended pre-approval); miners read only users."""
        if kind not in KINDS:
            raise ValueError(f"unknown decision kind: {kind}")
        clean = _scrub_obj({**(detail or {}), "actor": actor})
        cur = self._db.execute(
            "INSERT INTO decisions (ts, kind, session, cwd, project, subject, choice, detail, actor)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.clock() if ts is None else ts,
                kind,
                session,
                cwd,
                project if project is not None else self._project(cwd),
                scrub_text(subject),
                choice,
                json.dumps(clean, ensure_ascii=False),
                actor,
            ),
        )
        self._db.commit()
        return int(cur.lastrowid or 0)

    def project_for(self, cwd: str) -> str:
        """The project id ``record`` files a row under for ``cwd``."""
        return self._project(cwd)

    def update_detail(self, row_id: int, **kv: Any) -> None:
        """Merge ``kv`` (scrubbed) into one row's detail, e.g. what worked after a recorded tool error."""
        row = self._db.execute("SELECT detail FROM decisions WHERE id=?", (row_id,)).fetchone()
        if row is None:
            return
        detail = {**json.loads(row["detail"] or "{}"), **_scrub_obj(kv)}
        self._db.execute("UPDATE decisions SET detail=? WHERE id=?", (json.dumps(detail, ensure_ascii=False), row_id))
        self._db.commit()

    def query(
        self,
        kind: str | None = None,
        *,
        project: str | None = None,
        since: float | None = None,
        limit: int | None = None,
        actor: str | None = "user",
    ) -> list[dict[str, Any]]:
        """Rows are filtered to ``actor`` (default ``user``; ``None`` = every actor)."""
        sql, args = "SELECT * FROM decisions WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if project:
            sql += " AND project=?"
            args.append(project)
        if since is not None:
            sql += " AND ts>=?"
            args.append(since)
        if actor is not None:
            sql += " AND actor=?"
            args.append(actor)
        sql += " ORDER BY ts, id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = []
        for r in self._db.execute(sql, args):
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            rows.append(d)
        return rows

    def count(self, kind: str | None = None) -> int:
        if kind:
            return int(self._db.execute("SELECT COUNT(*) FROM decisions WHERE kind=?", (kind,)).fetchone()[0])
        return int(self._db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])

    def migrate_jsonl(self) -> int:
        """Import ``decisions.jsonl`` once (renamed to ``.migrated`` afterwards). Returns rows imported."""
        src = self.home / "decisions.jsonl"
        if not src.is_file():
            return 0
        n = 0
        for line in src.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = "plan" if row.get("tool") == "exit_plan" else "approval"
            self.record(
                kind,
                session=row.get("session", ""),
                cwd=row.get("cwd", ""),
                subject=row.get("pattern", ""),
                choice=row.get("choice", ""),
                detail={"tool": row.get("tool", ""), "migrated": True},
                ts=float(row.get("ts") or 0),
            )
            n += 1
        src.rename(src.with_suffix(".jsonl.migrated"))
        return n
