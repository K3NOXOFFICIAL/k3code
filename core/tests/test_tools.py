"""Tool registry and implementation tests."""

from __future__ import annotations

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
    assert set(names) == {"read", "write", "edit", "bash", "grep", "glob", "todo"}
    for name in names:
        spec, handler = reg.get(name)
        assert spec.name == name
        assert spec.parameters is not None
        assert callable(handler)


def test_side_effect_flags():
    reg = build_registry()
    pure = {"read", "grep", "glob", "todo"}
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
