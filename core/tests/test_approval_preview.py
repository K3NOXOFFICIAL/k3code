"""Edit and write approvals show what will change (issue #131).

The approval request carried only ``command: "edit <path>"``: a person approved file changes blind.
"""

from __future__ import annotations

from pathlib import Path

from k3code.providers.types import ToolCall
from k3code.tools import preview as preview_mod
from k3code.tools import tool_edit
from k3code.tools.preview import MAX_LINES, change_preview
from test_permissions_gateway import call, make_server, run_turn


def test_an_edit_preview_is_the_unified_diff_the_edit_will_make(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import os\nx = 1\nprint(x)\n")
    args = {"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"}
    text = change_preview("edit", args, tmp_path)
    assert text is not None
    assert text.splitlines()[:3] == ["--- a/app.py", "+++ b/app.py", "@@ -1,3 +1,3 @@"]
    assert "-x = 1" in text.splitlines() and "+x = 2" in text.splitlines() and " import os" in text.splitlines()
    assert (tmp_path / "app.py").read_text() == "import os\nx = 1\nprint(x)\n"  # nothing written


async def test_an_edit_preview_matches_what_the_edit_writes_even_for_a_fuzzy_match(tmp_path: Path) -> None:
    # "x = 1" is not in the file ("x  =  1" is): a plain old→new string swap would show nothing, the edit tool's own
    # matcher (whitespace-normalised) rewrites the line, and the preview is exactly that change
    before = "import os\nx  =  1\nprint(x)\n"
    (tmp_path / "a.py").write_text(before)
    args = {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}
    text = change_preview("edit", args, tmp_path)
    result = await tool_edit(args, cwd=tmp_path)
    assert result["strategy"] != "exact"
    after = (tmp_path / "a.py").read_text()
    assert text == preview_mod._diff(before, after, "a.py")
    assert "-x  =  1" in (text or "").splitlines() and "+x = 2" in (text or "").splitlines()


def test_a_failing_edit_says_so(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    text = change_preview("edit", {"path": "a.py", "old_string": "nope", "new_string": "y"}, tmp_path)
    assert text is not None and text.startswith("(this edit will fail:")


def test_a_write_preview_diffs_an_existing_file_and_lists_a_new_one(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("one\ntwo\n")
    text = change_preview("write", {"path": "old.txt", "content": "one\nTWO\n"}, tmp_path) or ""
    assert "-two" in text.splitlines() and "+TWO" in text.splitlines()
    text = change_preview("write", {"path": "new/n.txt", "content": "a\nb\n"}, tmp_path) or ""
    assert text.splitlines() == ["new file new/n.txt: 2 lines", "+a", "+b"]


def test_previews_are_capped(tmp_path: Path) -> None:
    text = change_preview("write", {"path": "big.txt", "content": "x\n" * (MAX_LINES * 3)}, tmp_path) or ""
    lines = text.splitlines()
    assert len(lines) == MAX_LINES + 1 and lines[-1] == f"… {MAX_LINES * 2 + 1} more lines"


def test_a_huge_or_binary_file_is_not_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(preview_mod, "MAX_FILE_BYTES", 10)
    (tmp_path / "big.txt").write_text("x" * 100)
    assert "too large" in (change_preview("write", {"path": "big.txt", "content": "y"}, tmp_path) or "")
    monkeypatch.setattr(preview_mod, "MAX_FILE_BYTES", 2_000_000)
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00binary")
    assert "not UTF-8" in (change_preview("write", {"path": "bin.dat", "content": "y"}, tmp_path) or "")


def test_other_tools_and_malformed_calls_get_no_diff(tmp_path: Path) -> None:
    assert change_preview("bash", {"command": "ls"}, tmp_path) is None
    (tmp_path / "a.py").write_text("x\n")
    assert (change_preview("edit", {"path": "a.py"}, tmp_path) or "").startswith("(no preview")  # no old_string
    assert (
        change_preview("edit", {"path": "gone.py", "old_string": "a", "new_string": "b"}, tmp_path) or ""
    ).startswith("(this edit will fail: File not found")


async def test_the_gateway_puts_the_preview_into_the_approval_request(tmp_path: Path) -> None:
    from k3code.gateway.server import _change_preview

    (tmp_path / "a.txt").write_text("old\n")
    text = await _change_preview("write", {"path": "a.txt", "content": "new\n"}, tmp_path)
    assert text is not None and "+new" in text.splitlines()
    assert await _change_preview("bash", {"command": "ls"}, tmp_path) is None


def _write_a_txt() -> ToolCall:
    return ToolCall(id="w", name="write", arguments={"path": "a.txt", "content": "new\n"})


async def test_the_approval_frame_carries_the_preview(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("old\n")
    server, _ = make_server(tmp_path, [_write_a_txt(), "ok"], monkeypatch)  # default mode: a write asks
    await call(server, "session.create", {"cwd": str(tmp_path)})
    seen = await run_turn(server, "go", [{"choice": "once"}])
    assert len(seen) == 1 and seen[0]["params"]["command"] == "write a.txt"
    assert seen[0]["params"]["preview"].splitlines() == ["--- a/a.txt", "+++ b/a.txt", "@@ -1 +1 @@", "-old", "+new"]
    assert (tmp_path / "a.txt").read_text() == "new\n"


async def test_a_preview_that_fails_never_blocks_the_prompt(tmp_path: Path, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("preview exploded")

    monkeypatch.setattr(preview_mod, "change_preview", broken)
    (tmp_path / "a.txt").write_text("old\n")
    server, _ = make_server(tmp_path, [_write_a_txt(), "ok"], monkeypatch)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    seen = await run_turn(server, "go", [{"choice": "once"}])
    # the person is still asked, just without a diff, and their answer still applies
    assert len(seen) == 1 and seen[0]["params"]["command"] == "write a.txt"
    assert "preview" not in seen[0]["params"]
    assert (tmp_path / "a.txt").read_text() == "new\n"
