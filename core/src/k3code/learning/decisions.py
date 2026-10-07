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

KINDS = ("approval", "model_switch", "proposal", "plan", "interrupt", "undo", "scope", "config")

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

_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|Bearer\s+\S+|"
                     r"(?i:(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+))")


def scrub(text: str) -> str:
    """Strip anything that looks like a credential before it reaches the log."""
    return _SECRET.sub("[redacted]", text)


def project_id(cwd: str | Path) -> str:
    """Stable id for a project: the git remote URL when there is one, else a hash of the path."""
    p = Path(cwd)
    try:
        out = subprocess.run(["git", "-C", str(p), "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, timeout=3, check=False)
        url = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        url = ""
    if url:
        url = re.sub(r"^[a-z+]+://([^@/]+@)?", "", url)  # drop scheme + userinfo (may hold a token)
        return "git:" + url.removesuffix(".git")
    return "path:" + hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:12]


class DecisionLog:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.home = Path(home)
        self.path = self.home / "learning" / "decisions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._pid_cache: dict[str, str] = {}
        self.migrate_jsonl()

    def close(self) -> None:
        self._db.close()

    def _project(self, cwd: str) -> str:
        if not cwd:
            return ""
        if cwd not in self._pid_cache:
            self._pid_cache[cwd] = project_id(cwd)
        return self._pid_cache[cwd]

    def record(self, kind: str, *, session: str = "", cwd: str = "", subject: str = "", choice: str = "",
               detail: dict[str, Any] | None = None, ts: float | None = None, project: str | None = None) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown decision kind: {kind}")
        clean = json.loads(scrub(json.dumps(detail or {}, ensure_ascii=False)))
        cur = self._db.execute(
            "INSERT INTO decisions (ts, kind, session, cwd, project, subject, choice, detail) VALUES (?,?,?,?,?,?,?,?)",
            (self.clock() if ts is None else ts, kind, session, cwd,
             project if project is not None else self._project(cwd),
             scrub(subject), choice, json.dumps(clean, ensure_ascii=False)),
        )
        self._db.commit()
        return int(cur.lastrowid or 0)

    def query(self, kind: str | None = None, *, project: str | None = None, since: float | None = None,
              limit: int | None = None) -> list[dict[str, Any]]:
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
            self.record(kind, session=row.get("session", ""), cwd=row.get("cwd", ""),
                        subject=row.get("pattern", ""), choice=row.get("choice", ""),
                        detail={"tool": row.get("tool", ""), "migrated": True}, ts=float(row.get("ts") or 0))
            n += 1
        src.rename(src.with_suffix(".jsonl.migrated"))
        return n
