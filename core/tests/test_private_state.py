"""File modes do not come from the service unit's UMask: files the tools write into a project follow the user's
umask, and k3code's private state is 0600/0700 whatever the umask."""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from k3code import memory, paths
from k3code.gateway.sessions import SessionStore
from k3code.learning.decisions import DecisionLog
from k3code.reliability.journal import ToolJournal
from k3code.setup import state
from k3code.tools import tool_edit, tool_write
from k3code.usage import UsageDB


@contextlib.contextmanager
def umask(value: int) -> Iterator[None]:
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.mark.parametrize(("mask", "file_mode", "dir_mode"), [(0o022, 0o644, 0o755), (0o077, 0o600, 0o700)])
async def test_tool_writes_follow_the_umask(tmp_path: Path, mask: int, file_mode: int, dir_mode: int) -> None:
    with umask(mask):
        await tool_write({"path": "pkg/new.py", "content": "x = 1\n"}, cwd=tmp_path)
    assert mode(tmp_path / "pkg" / "new.py") == file_mode
    assert mode(tmp_path / "pkg") == dir_mode
    assert [p.name for p in (tmp_path / "pkg").iterdir()] == ["new.py"]  # no temp file left behind


async def test_an_edit_keeps_the_files_own_mode(tmp_path: Path) -> None:
    f = tmp_path / "run.sh"
    f.write_text("echo a\n")
    f.chmod(0o750)
    with umask(0o077):
        await tool_edit({"path": "run.sh", "old_string": "echo a", "new_string": "echo b"}, cwd=tmp_path)
    assert mode(f) == 0o750 and f.read_text() == "echo b\n"


def test_private_state_is_0600_and_0700_whatever_the_umask(tmp_path: Path) -> None:
    home = paths.home()
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    with umask(0):  # the loosest umask: every mode below is set explicitly, not inherited
        stores = [SessionStore(home / "sessions.db"), UsageDB(home / "usage.db"), DecisionLog(home)]
        journal = ToolJournal(home, "s1")
        journal.record_intent("c1", "read", {"path": "x"}, side_effect=False)
        journal.close()
        learned = memory.write_learned(project, ["uses uv"])
        state.save_state({"completed": ["provider"], "data": {}})
    files = [
        home / "sessions.db",
        home / "usage.db",
        home / "learning" / "decisions.db",
        home / "journal" / "s1.jsonl",
        learned,
        state.state_path(),
    ]
    for f in files:
        assert mode(f) == 0o600, f
    for d in (home / "learning", home / "journal", learned.parent):
        assert mode(d) == 0o700, d
    for s in stores:
        with contextlib.suppress(AttributeError):
            s.close()


def test_existing_private_files_are_tightened(tmp_path: Path) -> None:
    db = paths.home() / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.touch()
    db.chmod(0o644)  # written by an older daemon or the CLI under umask 0022
    SessionStore(db)
    assert mode(db) == 0o600
