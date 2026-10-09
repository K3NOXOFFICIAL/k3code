"""The write tool creates files with the mode the umask allows (it made every new file 0600), and write/edit keep the
mode of a file that already exists."""

from __future__ import annotations

import os
import stat

import pytest

from k3code.tools import tool_edit, tool_write


@pytest.fixture
def umask():
    def set_mask(mask: int) -> None:
        os.umask(mask)

    old = os.umask(0o022)
    yield set_mask
    os.umask(old)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize(("mask", "expected"), [(0o022, 0o644), (0o077, 0o600), (0o002, 0o664)])
async def test_a_new_file_gets_the_umask_mode(tmp_path, umask, mask, expected):
    umask(mask)
    res = await tool_write({"path": "sub/new.txt", "content": "hi\n"}, cwd=tmp_path)
    assert res["ok"] is True
    assert _mode(tmp_path / "sub" / "new.txt") == expected


async def test_write_and_edit_keep_an_existing_files_mode(tmp_path, umask):
    umask(0o022)
    script = tmp_path / "run.sh"
    script.write_text("echo one\n")
    script.chmod(0o750)
    await tool_write({"path": "run.sh", "content": "echo two\n"}, cwd=tmp_path)
    assert _mode(script) == 0o750
    res = await tool_edit({"path": "run.sh", "old_string": "two", "new_string": "three"}, cwd=tmp_path)
    assert res.get("ok") is True
    assert _mode(script) == 0o750 and script.read_text() == "echo three\n"


async def test_no_temporary_file_is_left_behind(tmp_path, umask):
    await tool_write({"path": "a.txt", "content": "x"}, cwd=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


async def test_a_private_files_temp_copy_is_never_wider_than_0600(tmp_path, umask, monkeypatch):
    umask(0o022)
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=old\n")
    secret.chmod(0o600)
    seen: list[int] = []
    real_fsync = os.fsync

    def spy(fd):
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))  # the temp file's mode while it holds the full content
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    res = await tool_write({"path": ".env", "content": "TOKEN=new\n"}, cwd=tmp_path)
    assert res["ok"] is True
    assert seen == [0o600]
    assert _mode(secret) == 0o600
