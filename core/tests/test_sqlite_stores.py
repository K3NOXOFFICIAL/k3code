"""Every SQLite store under $K3CODE_HOME runs in WAL mode with a 10 s busy timeout.

The daemon, a standalone TUI gateway and the CLI open the same files at once. In rollback-journal mode an open read
transaction in one process blocked another's commit until it failed with "database is locked"."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from k3code.artifacts import ArtifactStore
from k3code.automation.store import AutomationDB
from k3code.blockers import BlockerStore
from k3code.gateway.sessions import SessionStore
from k3code.learning.decisions import DecisionLog
from k3code.usage import UsageDB

STORES = {
    "sessions": lambda home: (SessionStore(home / "sessions.db"), home / "sessions.db"),
    "usage": lambda home: (UsageDB(home / "usage.db"), home / "usage.db"),
    "artifacts": lambda home: (ArtifactStore(home / "artifacts.db"), home / "artifacts.db"),
    "blockers": lambda home: (BlockerStore(home / "blockers.db"), home / "blockers.db"),
    "automation": lambda home: (AutomationDB(home / "automation.db"), home / "automation.db"),
    "decisions": lambda home: (DecisionLog(home), home / "learning" / "decisions.db"),
}


@pytest.mark.parametrize("name", sorted(STORES))
def test_store_uses_wal_and_a_busy_timeout(tmp_path, name):
    store, path = STORES[name](tmp_path)
    try:
        assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        other = sqlite3.connect(path)  # journal_mode=WAL is stored in the file: a fresh connection sees it
        try:
            assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            other.close()
    finally:
        store.close()


def test_a_reader_holding_a_transaction_does_not_block_a_writer(tmp_path):
    """A second process reading the sessions (`k3code agents`, a TUI's own gateway) while the daemon saves one."""
    store = SessionStore(tmp_path / "sessions.db")
    sess = store.create(title="first")
    reader = sqlite3.connect(tmp_path / "sessions.db", timeout=0)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1  # read lock held from here on
    errors: list[BaseException] = []

    def write() -> None:
        try:
            writer = SessionStore(tmp_path / "sessions.db")
            sess.title = "renamed"
            writer.save(sess)
            writer.close()
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(e)

    t = threading.Thread(target=write)
    start = time.monotonic()
    t.start()
    t.join(timeout=30)
    elapsed = time.monotonic() - start
    reader.rollback()
    reader.close()
    store.close()
    assert not errors, errors
    assert elapsed < 3, f"the writer waited {elapsed:.1f}s for a reader"
    check = SessionStore(tmp_path / "sessions.db")
    assert check.get(sess.session_id).title == "renamed"
    check.close()
