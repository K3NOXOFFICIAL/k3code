"""Tool registry and implementation tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from k3code.tools import build_registry, tool_bash, tool_edit, tool_glob, tool_grep, tool_read, tool_write

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ── Registry tests ─────────────────────────────────────────────────────


def test_registry_has_all_tools():
    reg = build_registry()
    names = reg.names()
    assert set(names) == {"read", "write", "edit", "bash", "grep", "glob", "todo", "exit_plan"}
    for name in names:
        spec, handler = reg.get(name)
        assert spec.name == name
        assert spec.parameters is not None
        assert callable(handler)


def test_side_effect_flags():
    reg = build_registry()
    pure = {"read", "grep", "glob", "todo", "exit_plan"}
    side_effect = {"write", "edit", "bash"}
    for name in pure:
        spec, _ = reg.get(name)
        assert spec.side_effect is False, f"{name} should be pure"
    for name in side_effect:
        spec, _ = reg.get(name)
        assert spec.side_effect is True, f"{name} should have side_effect"


# ── Tool tests ─────────────────────────────────────────────────────────


async def test_read_file(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("line1\nline2\nline3\n")
    result = await tool_read({"path": str(f)}, cwd=temp_dir)
    assert "line1" in result["content"]
    assert result["lines"] == "1-3 of 3"


async def test_read_line_range(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("line1\nline2\nline3\nline4\nline5\n")
    result = await tool_read({"path": str(f), "start": 2, "end": 4}, cwd=temp_dir)
    assert result["content"] == "line2\nline3\nline4"
    assert result["lines"] == "2-4 of 5"


async def test_write_file(temp_dir):
    f = temp_dir / "new.txt"
    result = await tool_write({"path": str(f), "content": "hello"}, cwd=temp_dir)
    assert result["ok"] is True
    assert f.read_text() == "hello"


async def test_write_creates_parents(temp_dir):
    f = temp_dir / "sub" / "dir" / "file.txt"
    result = await tool_write({"path": str(f), "content": "nested"}, cwd=temp_dir)
    assert result["ok"] is True
    assert f.read_text() == "nested"


async def test_edit_exact_match(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("foo\nbar\nbaz\n")
    result = await tool_edit({"path": str(f), "old_string": "bar", "new_string": "BAR"}, cwd=temp_dir)
    assert result["ok"] is True
    assert result["replacements"] == 1
    assert f.read_text() == "foo\nBAR\nbaz\n"


async def test_edit_not_found(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("foo\nbar\n")
    result = await tool_edit({"path": str(f), "old_string": "missing", "new_string": "x"}, cwd=temp_dir)
    assert "error" in result
    assert "Could not find" in result["error"]


async def test_edit_ambiguous(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("foo\nfoo\n")
    result = await tool_edit({"path": str(f), "old_string": "foo", "new_string": "bar"}, cwd=temp_dir)
    assert "error" in result
    assert "Found 2 matches" in result["error"]


async def test_edit_replace_all(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("foo\nfoo\n")
    result = await tool_edit(
        {"path": str(f), "old_string": "foo", "new_string": "bar", "replace_all": True}, cwd=temp_dir
    )
    assert result["ok"] is True
    assert result["replacements"] == 2
    assert f.read_text() == "bar\nbar\n"


async def test_bash_simple(temp_dir):
    result = await tool_bash({"command": "echo hello"}, cwd=temp_dir)
    assert result["stdout"].strip() == "hello"
    assert result["exit_code"] == 0


async def test_bash_timeout_kill(temp_dir):
    # Sleep longer than timeout
    result = await tool_bash({"command": "sleep 10", "timeout": 0.1}, cwd=temp_dir)
    assert "error" in result
    assert "timed out" in result["error"].lower()
    assert result["exit_code"] == -1


async def test_grep_finds(temp_dir):
    (temp_dir / "a.py").write_text("def foo():\n    pass\n")
    (temp_dir / "b.py").write_text("def bar():\n    pass\n")
    result = await tool_grep({"pattern": "def foo", "path": "."}, cwd=temp_dir)
    assert "a.py" in result["matches"]
    assert "foo" in result["matches"]


async def test_grep_no_matches(temp_dir):
    (temp_dir / "a.py").write_text("x = 1\n")
    result = await tool_grep({"pattern": "def foo", "path": "."}, cwd=temp_dir)
    assert result["matches"] == ""


async def test_grep_pattern_that_looks_like_a_flag_is_searched_as_text(temp_dir):
    # Without -e/--, rg reads "--files" as its own option and lists file names instead of matching the line.
    (temp_dir / "notes.txt").write_text("the flag --files is literal here\n")
    result = await tool_grep({"pattern": "--files", "path": "."}, cwd=temp_dir)
    assert "notes.txt:1:the flag --files is literal here" in result["matches"]


async def test_glob_finds(temp_dir):
    (temp_dir / "a.py").write_text("")
    (temp_dir / "b.txt").write_text("")
    (temp_dir / "sub").mkdir(exist_ok=True)
    (temp_dir / "sub" / "c.py").write_text("")
    result = await tool_glob({"pattern": "*.py", "path": "."}, cwd=temp_dir)
    files = set(result["files"])
    assert "a.py" in files
    assert "sub/c.py" in files
    assert "b.txt" not in files


# ── Fuzzy match tests (via edit tool) ─────────────────────────────────


async def test_edit_whitespace_flexible(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("def foo():\n    return 1\n")
    # Extra spaces in old_string
    result = await tool_edit(
        {"path": str(f), "old_string": "def foo():  \n    return 1", "new_string": "def foo():\n    return 2"},
        cwd=temp_dir,
    )
    assert result["ok"] is True
    assert f.read_text() == "def foo():\n    return 2\n"


async def test_edit_indentation_flexible(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("  def foo():\n    return 1\n")
    # Less indentation in old_string
    result = await tool_edit(
        {"path": str(f), "old_string": "def foo():\n  return 1", "new_string": "def foo():\n  return 2"},
        cwd=temp_dir,
    )
    assert result["ok"] is True
    assert f.read_text() == "  def foo():\n    return 2\n"


# ── bash: bounded output, group kill on cancel/timeout (found by the long-run audit) ──


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie of a reaped-late child counts as gone
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


async def test_bash_keeps_the_whole_output_when_it_fits_the_capture(temp_dir):
    """The model's cut (clip_tool_results) happens later; the tool itself keeps everything it captured."""
    result = await tool_bash({"command": "head -c 12000 /dev/zero | tr '\\0' a"}, cwd=temp_dir)
    assert result["stdout"] == "a" * 12_000
    assert result["exit_code"] == 0


async def test_bash_capture_keeps_the_head_and_the_tail_and_counts_what_it_dropped(temp_dir):
    import k3code.tools as tools

    result = await tool_bash({"command": "seq 1 30000"}, cwd=temp_dir)
    full = "".join(f"{i}\n" for i in range(1, 30_001))
    dropped = len(full) - tools._KEEP_HEAD_BYTES - tools._KEEP_TAIL_BYTES
    expected = (
        full[: tools._KEEP_HEAD_BYTES]
        + f"\n... [truncated {dropped} bytes not kept by the capture] ...\n"
        + full[-tools._KEEP_TAIL_BYTES :]
    )
    assert result["stdout"] == expected
    assert result["stdout"].endswith("30000\n")


async def test_bash_flood_is_killed_instead_of_filling_daemon_memory(temp_dir, monkeypatch):
    """communicate() buffered every byte: `yes` took the daemon to 1 GB/s of RSS until the OOM killer ran."""
    import resource

    import k3code.tools as tools

    monkeypatch.setattr(tools, "MAX_OUTPUT_BYTES", 2 * 1024 * 1024)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = await asyncio.wait_for(tool_bash({"command": "yes", "timeout": 30}, cwd=temp_dir), 20)
    assert "Output limit exceeded" in result["error"] and result["exit_code"] == -9
    assert len(result["stdout"]) < tools._KEEP_HEAD_BYTES + tools._KEEP_TAIL_BYTES + 200  # head + tail + marker
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 100_000  # KB: far below the old GBs


async def test_bash_cancel_kills_the_whole_process_group(temp_dir):
    """/stop cancels the task; only the shell used to be waited on and `sleep 300` kept running."""
    pidfile = temp_dir / "child.pid"
    task = asyncio.create_task(
        tool_bash({"command": f"sleep 300 & echo $! > {pidfile}; wait", "timeout": 60}, cwd=temp_dir)
    )
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    child = int(pidfile.read_text())
    assert _alive(child)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert not _alive(child)


async def test_bash_timeout_returns_promptly_even_when_a_grandchild_holds_the_pipe(temp_dir):
    """wait() only finishes when the pipes close, so a background child used to hang the timeout path."""
    pidfile = temp_dir / "bg.pid"
    started = asyncio.get_running_loop().time()
    result = await asyncio.wait_for(
        tool_bash({"command": f"sleep 300 & echo $! > {pidfile}; sleep 300", "timeout": 0.5}, cwd=temp_dir), 15
    )
    assert "timed out" in result["error"] and result["exit_code"] == -1
    assert asyncio.get_running_loop().time() - started < 10
    await asyncio.sleep(0.3)
    assert not _alive(int(pidfile.read_text()))


# ── file tools: bytes outside an edit are preserved; writes are atomic ──


async def test_edit_keeps_crlf_line_endings(temp_dir):
    f = temp_dir / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = await tool_edit({"path": str(f), "old_string": "two\nthree", "new_string": "2\nthree"}, cwd=temp_dir)
    assert result["ok"] is True
    assert f.read_bytes() == b"one\r\n2\r\nthree\r\n"  # one edit must not rewrite every line ending


async def test_edit_leaves_mixed_line_endings_alone(temp_dir):
    f = temp_dir / "mixed.txt"
    f.write_bytes(b"a\r\nb\nc\r\n")
    result = await tool_edit({"path": str(f), "old_string": "b", "new_string": "B"}, cwd=temp_dir)
    assert result["ok"] is True and f.read_bytes() == b"a\r\nB\nc\r\n"


async def test_edit_refuses_a_file_that_is_not_utf8_instead_of_destroying_its_bytes(temp_dir):
    f = temp_dir / "latin1.txt"
    f.write_bytes("caf\xe9 au lait\n".encode("latin-1"))
    result = await tool_edit({"path": str(f), "old_string": "au", "new_string": "AU"}, cwd=temp_dir)
    assert "not valid UTF-8" in result["error"]
    assert f.read_bytes() == "caf\xe9 au lait\n".encode("latin-1")  # untouched, 0xE9 intact


async def test_write_is_atomic_and_keeps_the_mode(temp_dir):
    f = temp_dir / "run.sh"
    f.write_text("old\n")
    f.chmod(0o755)
    assert (await tool_write({"path": str(f), "content": "new\n"}, cwd=temp_dir))["ok"] is True
    assert f.read_text() == "new\n" and (f.stat().st_mode & 0o777) == 0o755
    assert [p.name for p in temp_dir.iterdir() if "k3tmp" in p.name] == []  # no temp file left behind


async def test_write_through_a_symlink_keeps_the_link(temp_dir):
    real = temp_dir / "real.txt"
    real.write_text("x")
    link = temp_dir / "link.txt"
    link.symlink_to(real)
    await tool_write({"path": str(link), "content": "y"}, cwd=temp_dir)
    assert link.is_symlink() and real.read_text() == "y"


async def test_read_numbers_lines_like_an_editor(temp_dir):
    f = temp_dir / "ff.txt"
    f.write_text("a\x0cb\nc\nd\n")  # a form feed is not a line break for editors (str.splitlines() splits on it)
    result = await tool_read({"path": str(f), "start": 2, "end": 2}, cwd=temp_dir)
    assert result["content"] == "c" and result["lines"] == "2-2 of 3"
