"""One way to open the SQLite stores under ``$K3CODE_HOME``.

The daemon, a standalone TUI gateway and the CLI (``k3code stats``, ``-p`` runs) open the same files at once. In the
default rollback-journal mode a reader's open transaction blocks a writer's commit, and two writers that both started
as readers fail at once with "database is locked". WAL lets readers and one writer run side by side, and the busy
timeout makes a second writer wait for the first instead of failing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from k3code.paths import private_file

#: Milliseconds a connection waits for another one's write lock before "database is locked".
BUSY_TIMEOUT_MS = 10_000


def connect(path: Path | str, **kwargs: Any) -> sqlite3.Connection:
    """``sqlite3.connect`` with WAL journaling and a 10 s busy timeout. The database is made 0600 first, whatever the
    umask (sqlite gives its -wal and -shm files the database's mode): the stores hold transcripts and usage."""
    private_file(Path(path))
    kwargs.setdefault("timeout", BUSY_TIMEOUT_MS / 1000)
    db = sqlite3.connect(str(path), **kwargs)
    db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA auto_vacuum=INCREMENTAL")  # only takes effect on a database with no tables yet
    db.execute("PRAGMA journal_mode=WAL")
    return db


#: :func:`compact` leaves a database alone while less than this share of its pages is free, or while the free
#: pages add up to less than ``COMPACT_MIN_FREE_BYTES``.
COMPACT_MIN_FREE_RATIO = 0.25
COMPACT_MIN_FREE_BYTES = 1 << 20
_AUTO_VACUUM_INCREMENTAL = 2


def compact(db: sqlite3.Connection) -> int:
    """Give the pages that deleted rows left behind back to the filesystem; returns the bytes freed (0 = left alone).

    Deleting a 100 MB transcript only moves its pages to the free list, so the file never shrinks. A database made
    before ``auto_vacuum`` was on is converted with one ``VACUUM``; after that the free pages are returned without
    rewriting the file. Call it with no transaction open."""
    db.commit()
    page_size = db.execute("PRAGMA page_size").fetchone()[0]
    pages = db.execute("PRAGMA page_count").fetchone()[0]
    free = db.execute("PRAGMA freelist_count").fetchone()[0]
    if not pages or free < pages * COMPACT_MIN_FREE_RATIO or free * page_size < COMPACT_MIN_FREE_BYTES:
        return 0
    if db.execute("PRAGMA auto_vacuum").fetchone()[0] != _AUTO_VACUUM_INCREMENTAL:
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("VACUUM")
    else:
        db.execute("PRAGMA incremental_vacuum").fetchall()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return (pages - db.execute("PRAGMA page_count").fetchone()[0]) * page_size
