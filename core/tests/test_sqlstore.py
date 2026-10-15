"""sqlstore.connect: the pragmas every store connection gets."""

from __future__ import annotations

from k3code import sqlstore

_NORMAL = 1


def test_connect_uses_wal_with_synchronous_normal(tmp_path):
    """Issue #164: FULL fsynced the WAL on every usage-row commit; NORMAL is safe in WAL mode."""
    db = sqlstore.connect(tmp_path / "x.db")
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == _NORMAL
    finally:
        db.close()
