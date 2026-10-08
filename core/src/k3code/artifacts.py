"""Artifact registry: files that sessions produced (plans, research, previews, review reports, dumps, bundles)."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KINDS = ("plan", "research", "preview", "review", "debug", "export", "other")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_ts ON artifacts(ts);
"""


@dataclass
class Artifact:
    id: str
    ts: float
    session: str
    kind: str
    title: str
    path: str

    @property
    def exists(self) -> bool:
        return Path(self.path).exists()


class ArtifactStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def register(self, kind: str, path: Path | str, *, title: str = "", session: str = "") -> Artifact:
        art = Artifact(uuid.uuid4().hex[:8], time.time(), session, kind if kind in KINDS else "other", title,
                       str(Path(path).resolve()))
        self._db.execute(
            "INSERT INTO artifacts (id, ts, session, kind, title, path) VALUES (?,?,?,?,?,?)",
            (art.id, art.ts, art.session, art.kind, art.title, art.path),
        )
        self._db.commit()
        return art

    def list(self, *, kind: str | None = None, session: str | None = None, limit: int = 50) -> list[Artifact]:
        q, args = "SELECT id, ts, session, kind, title, path FROM artifacts", []
        where = []
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if session:
            where.append("session = ?")
            args.append(session)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY ts DESC LIMIT ?"
        return [Artifact(*row) for row in self._db.execute(q, [*args, limit])]

    def get(self, ident: str) -> Artifact | None:
        row = self._db.execute(
            "SELECT id, ts, session, kind, title, path FROM artifacts WHERE id = ? OR id LIKE ?",
            (ident, ident + "%"),
        ).fetchone()
        return Artifact(*row) if row else None

    def close(self) -> None:
        self._db.close()


def register_artifact(ctx: Any, kind: str, path: Path | str, *, title: str = "", session: str = "") -> str | None:
    """Producers call this; a context without a registry (unit tests) is ignored. Returns the artifact id."""
    store = getattr(ctx, "artifacts", None)
    if store is None:
        return None
    try:
        return store.register(kind, path, title=title, session=session or "").id
    except Exception:  # noqa: BLE001 - registering must never break the producer
        logger.warning("artifact registration failed", exc_info=True)
        return None


class AlreadyPublished(Exception):
    """The published folder already holds a file with that name; ``--force`` replaces it."""


def publish_file(src: Path, dest_dir: Path, *, force: bool = False) -> Path:
    """Copy ``src`` into ``dest_dir`` under its own name. A local copy only: nothing is uploaded anywhere."""
    dest = dest_dir / src.name
    if dest.exists() and not force:
        raise AlreadyPublished(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def slugify(text: str, limit: int = 40) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:limit].strip("-") or "untitled"


def write_artifact_file(ctx: Any, kind: str, directory: Path, title: str, body: str, *, session: str = "") -> Path:
    """``<directory>/<ts>-<slug>.md`` plus a registry row."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(title)}.md"
    n = 1
    while path.exists():
        n += 1
        path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(title)}-{n}.md"
    path.write_text(body, encoding="utf-8")
    register_artifact(ctx, kind, path, title=title, session=session)
    return path
