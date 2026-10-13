"""How a tool result reaches the model and the TUI: free-text tools are plain text, not a Python dict repr."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from k3code.agent.loop import AgentLoop
from k3code.providers.types import Message, ToolCall
from k3code.router import Router, build_chain
from k3code.tools import format_tool_result, tool_glob
from test_agent import FakeProvider, make_done_event


def test_bash_output_is_plain_text_and_only_says_more_when_there_is_more():
    assert format_tool_result({"stdout": "a\nb\n", "stderr": "", "exit_code": 0}) == "a\nb"
    assert format_tool_result({"stdout": "", "stderr": "", "exit_code": 0}) == "(no output)"
    failed = format_tool_result({"stdout": "partial\n", "stderr": "boom\n", "exit_code": 2})
    assert failed == "partial\n[stderr]\nboom\n[exit code 2]"
    timed_out = format_tool_result(
        {"error": "Command timed out after 5s", "stdout": "x", "stderr": "", "exit_code": -1}
    )
    assert timed_out == "x\n[Command timed out after 5s]\n[exit code -1]"


def test_quotes_backslashes_and_newlines_are_not_escaped():
    out = format_tool_result({"stdout": "print('hi')\npath = C:\\tmp\\n\n", "stderr": "", "exit_code": 0})
    assert out == "print('hi')\npath = C:\\tmp\\n"  # exactly what the command printed
    assert "\\'" not in out and "\\\\" not in out


def test_grep_and_glob_are_plain_lists_and_say_when_empty():
    assert format_tool_result({"matches": "a.py:3:foo\nb.py:9:foo"}) == "a.py:3:foo\nb.py:9:foo"
    assert format_tool_result({"matches": ""}) == "(no matches)"
    assert format_tool_result({"matches": "a.py:1:x", "warnings": "2 files skipped"}) == "a.py:1:x\n[2 files skipped]"
    assert format_tool_result({"files": ["a.py", "src/b.py"]}) == "a.py\nsrc/b.py"
    assert format_tool_result({"files": []}) == "(no files)"  # a plain miss needs no explanation
    assert format_tool_result({"files": [], "note": "why"}) == "(no files)\n[why]"
    # rg's stderr lines (an unreadable directory) are one bracket each, not a list repr
    denied = ["rg: ./a: Permission denied", "rg: ./b: Permission denied"]
    shown = format_tool_result({"matches": "", "warnings": denied})
    assert shown == "[rg: ./a: Permission denied]\n[rg: ./b: Permission denied]"


async def test_an_empty_glob_names_a_skipped_folder_the_pattern_asks_for(tmp_path):
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "main.tf").write_text("x\n")
    shown = format_tool_result(await tool_glob({"pattern": ".terraform/*.tf"}, cwd=tmp_path))
    assert ".terraform is skipped" in shown and "path=.terraform" in shown
    assert format_tool_result(await tool_glob({"pattern": "*.tf"}, cwd=tmp_path)) == "(no files)"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_glob_leaves_out_a_cache_folder_even_when_git_tracks_its_files(tmp_path):
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "main.tf").write_text("x\n")
    (tmp_path / "app.tf").write_text("x\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path, check=True, capture_output=True, text=True)
    assert ".terraform/main.tf" in tracked.stdout.splitlines()
    assert (await tool_glob({"pattern": "*.tf"}, cwd=tmp_path))["files"] == ["app.tf"]


def test_write_edit_and_plain_errors_are_one_readable_line():
    assert format_tool_result({"ok": True, "path": "/p/a.txt"}) == "Wrote /p/a.txt"
    assert format_tool_result({"ok": True, "replacements": 1, "strategy": "exact"}) == "Edited: 1 replacement"
    assert format_tool_result({"ok": True, "replacements": 3, "strategy": "exact"}) == "Edited: 3 replacements"
    fuzzy = format_tool_result({"ok": True, "replacements": 1, "strategy": "line_trimmed"})
    assert fuzzy == "Edited: 1 replacement (matched by line_trimmed, not exactly)"
    assert format_tool_result({"error": "File not found: x"}) == "Error: File not found: x"


def test_everything_else_is_unchanged():
    assert format_tool_result({"content": "line 1\nline 2", "lines": "1-2"}) == "line 1\nline 2"
    assert format_tool_result({"content": ""}) == ""
    # an unknown shape is JSON, not a repr
    assert format_tool_result({"ok": True, "note": "n"}) == '{"ok": true, "note": "n"}'
    assert format_tool_result({"error": "x", "extra": 1}) == '{"error": "x", "extra": 1}'


def test_unknown_shape_is_json_and_survives_non_serializable_values():
    big = {"note": "é" * 10, "items": list(range(3)), "path": Path("/tmp/x")}
    out = format_tool_result(big)
    assert json.loads(out)["items"] == [0, 1, 2]
    assert "é" * 10 in out  # not \u-escaped
    assert "/tmp/x" in out


def test_oversized_result_is_clipped_head_and_tail():
    from k3code.tools import MAX_STORED_RESULT_CHARS

    big = "H" * 600_000 + "M" * 600_000 + "T" * 600_000
    out = format_tool_result({"stdout": big, "stderr": "", "exit_code": 0})
    assert len(out) < MAX_STORED_RESULT_CHARS + 300
    assert out.startswith("H") and out.endswith("T")
    assert "truncated" in out


def test_small_result_is_not_clipped():
    assert format_tool_result({"content": "x" * 1000}) == "x" * 1000


@pytest.mark.asyncio
async def test_the_model_receives_the_plain_text_of_a_bash_call(tmp_path):
    call = ToolCall(id="c1", name="bash", arguments={"command": "printf 'one\\ntwo\\n'; echo oops >&2; exit 3"})
    provider = FakeProvider(
        [
            [make_done_event(Message(role="assistant", content=None, tool_calls=[call]))],
            [make_done_event(Message(role="assistant", content="done", tool_calls=[]))],
        ]
    )
    loop = AgentLoop(
        Router(build_chain([provider], [["m"]]), max_retries=0),
        system_prompt="test",
        max_turns=5,
        permission_mode="yolo",
        cwd=tmp_path,
    )
    tool_messages = [e.message async for e in loop.run("go") if e.type == "done" and e.message.role == "tool"]
    assert [m.content for m in tool_messages] == ["one\ntwo\n[stderr]\noops\n[exit code 3]"]
