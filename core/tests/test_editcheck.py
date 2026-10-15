"""edit and write end with a syntax check of the file (``syntax: ok`` or the error with its line), so the model fixes
a broken file in the next call instead of finding out when it runs the tests, or never."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from k3code.config import Settings
from k3code.providers.types import ToolCall
from k3code.tools import editcheck
from k3code.tools.editcheck import render, syntax_check
from test_stale_write import run_script
from test_token_context_budget import make_loop
from test_wire_clip import Recorder, text_reply, tool_reply


async def _check(tmp_path: Path, name: str, text: str) -> str | None:
    f = tmp_path / name
    f.write_text(text)
    return await syntax_check(f)


async def test_python_json_yaml_and_toml_are_parsed_in_process(tmp_path: Path):
    assert await _check(tmp_path, "ok.py", "def f():\n    return 1\n") == ""
    found = await _check(tmp_path, "bad.py", "def f(:\n    pass\n")
    assert found and "line 1" in found
    assert await _check(tmp_path, "ok.json", '{"a": [1, 2]}') == ""
    assert "line 1" in (await _check(tmp_path, "bad.json", '{"a": 1,}') or "")
    assert await _check(tmp_path, "ok.yaml", "a:\n  - 1\n") == ""
    found = await _check(tmp_path, "bad.yaml", "a: [1\n")
    assert found and "(line 2, column 1)" in found and len(found) < 120  # the mark, not yaml's three-line context
    assert await _check(tmp_path, "ok.toml", "[a]\nb = 1\n") == ""
    assert "line 1" in (await _check(tmp_path, "bad.toml", "a = \n") or "")


async def test_other_types_huge_files_and_unreadable_ones_are_not_checked(tmp_path: Path, monkeypatch):
    assert await _check(tmp_path, "notes.md", "# hi\n") is None
    assert await _check(tmp_path, "data.bin", "x") is None
    monkeypatch.setattr(editcheck, "CHECK_MAX_BYTES", 10)
    assert await _check(tmp_path, "big.py", "x = 1\n" * 10) is None  # the model is not kept waiting for a parse
    assert await syntax_check(tmp_path / "missing.py") is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_javascript_goes_through_node_check(tmp_path: Path):
    assert await _check(tmp_path, "ok.js", "const x = 1;\n") == ""
    found = await _check(tmp_path, "bad.js", "function f( {\n}\n")
    assert found and "SyntaxError" in found and "line" in found


async def test_without_node_javascript_is_not_checked(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(editcheck.shutil, "which", lambda _name: None)
    assert await _check(tmp_path, "x.js", "function f( {") is None


def test_render():
    assert render(None) == ""
    assert render("") == "syntax: ok"
    assert render("invalid syntax (line 3)") == "syntax: invalid syntax (line 3). Fix it before you go on."


async def test_the_check_is_appended_to_edit_and_write_results(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("def f():\n    return 1\n")
    script = [
        ToolCall(id="r", name="read", arguments={"path": str(f)}),
        ToolCall(id="e", name="edit", arguments={"path": str(f), "old_string": "return 1", "new_string": "return ("}),
        ToolCall(id="w", name="write", arguments={"path": str(tmp_path / "n.json"), "content": '{"ok": true}'}),
        ToolCall(id="t", name="write", arguments={"path": str(tmp_path / "n.txt"), "content": "plain"}),
    ]
    results = await run_script(tmp_path, script)
    assert results["e"].startswith("Edited: 1 replacement")
    assert "\nsyntax: " in results["e"] and "line 2" in results["e"] and "Fix it" in results["e"]
    assert f.read_text() == "def f():\n    return (\n"  # the check never blocks the edit itself
    assert results["w"] == f"Wrote {tmp_path / 'n.json'}\nsyntax: ok"
    assert results["t"] == f"Wrote {tmp_path / 'n.txt'}"  # no check for the type: nothing appended


async def test_the_check_can_be_turned_off(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("x = (\n")
    provider = Recorder([tool_reply(ToolCall(id="w", name="write", arguments={"path": str(f), "content": "y = (\n"}))])
    provider.replies.append(text_reply("done"))
    loop = make_loop(tmp_path, provider, window=None)
    loop.file_stamps.record(f)
    loop.syntax_checks = False
    async for _ in loop.run("go"):
        pass
    (result,) = [m.content for m in loop.turn_messages if m.role == "tool"]
    assert result == f"Wrote {f}"
    assert editcheck.enabled(Settings()) is True
    assert editcheck.enabled(Settings(tools={"syntax_check": False})) is False
