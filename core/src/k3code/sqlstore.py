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
    db.execute("PRAGMA journal_mode=WAL")
    return db
