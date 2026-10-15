"""A failed ``edit`` tells the model what to send next: the closest region of the file, numbered as ``read`` numbers
lines, with the whitespace difference named; an ambiguous old_string lists every match's line."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.tools import format_tool_result, tool_edit, tool_read
from k3code.tools.editmatch import closest_region, no_match_hint

SOURCE = (
    "import os\n\nclass A:\n    def run(self):\n        x = 1\n\n        return x\n\n"
    "    def stop(self):\n        return None\n"
)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    f = tmp_path / "a.py"
    f.write_text(SOURCE)
    return f


async def _edit(f: Path, old: str, new: str = "pass", **kw) -> str:
    result = await tool_edit({"path": str(f), "old_string": old, "new_string": new, **kw}, cwd=f.parent)
    assert "error" in result and "ok" not in result
    return format_tool_result(result)


async def test_no_match_shows_the_closest_region_numbered_like_read(src: Path):
    text = await _edit(src, "def run(self):\n    x = 1\n    return x")  # the blank line inside is missing
    assert text.startswith("Error: Could not find a match")
    assert "Closest match at lines 4-7" in text
    assert "     4\t    def run(self):" in text and "     7\t        return x" in text  # read's number<TAB>line
    assert "     2\t" in text and "     9\t    def stop(self):" in text  # two lines of context around it
    assert "blank lines differ" in text
    assert "without the number and tab" in text
    assert src.read_text() == SOURCE


def test_the_hint_names_the_whitespace_difference():
    # the fuzzy strategies take most whitespace drift; the hint covers what reaches it (and the plain-error path)
    tabs = "if a:\n\tb = 1\n\tc = 2\n\td = 3\n"
    text = no_match_hint("    b = 1\n    c = 2\n    d = 3", tabs)
    assert "Closest match at lines 2-4" in text and "the file indents with tabs" in text
    text = no_match_hint("  b = 1\n  c = 2\n  d = 3", "if a:\n    b = 1\n    c = 2\n    d = 3\n")
    assert "indentation differs (2 vs 4 spaces in the file)" in text
    text = no_match_hint("b = 1\r\nc = 2", "b = 1\nc = 2\n")
    assert "CRLF" in text
    text = no_match_hint("b = 1  \nc = 2", "b = 1\nc = 2\n")
    assert "trailing whitespace differs" in text
    text = no_match_hint("b = 1\nc = 9", "b = 1\nc = 2\n")
    assert "Only whitespace" not in text and "Copy old_string from these lines exactly" in text


async def test_nothing_alike_says_so_instead_of_a_random_region(src: Path):
    text = await _edit(src, "SELECT * FROM users WHERE id = ?")
    assert "The file holds nothing like it" in text and "Closest match" not in text


def test_closest_region_prefers_the_exact_stripped_window():
    content = "a\nb\nc\nd\ne\nb\nc\nd\n"
    assert closest_region("  b\n  c\n  d", content) == (2, 4, 1.0)  # the first of two equal windows
    assert closest_region("b\nc", content)[:2] == (2, 3)
    assert closest_region("zzz\nyyy", content) is None
    assert closest_region("", content) is None and closest_region("  \n ", content) is None


def test_closest_region_scales_to_a_big_file():
    lines = [f"line {i} = {i * 7 % 13}" for i in range(60_000)]
    lines[45_000:45_003] = ["def target(a, b):", "    return a + b", ""]
    found = closest_region("def target(a, b):\n    return a - b\n", "\n".join(lines) + "\n")
    assert found is not None and found[:2] == (45_001, 45_002)


async def test_ambiguous_old_string_is_refused_with_every_match_line(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("def a():\n    return x\n\ndef b():\n    return x\n")
    text = await _edit(f, "    return x")
    assert "matches 2 places" in text and "nothing was changed" in text
    assert "     2\t    return x" in text and "     5\t    return x" in text
    assert "replace_all" in text
    assert f.read_text().count("return x") == 2
    # a multi-line match names its line range
    text = await _edit(f, "():\n    return x")
    assert "   1-2\tdef a():" in text and "   4-5\tdef b():" in text


async def test_ambiguous_old_string_with_replace_all_changes_every_match(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("x\nx\nx\n")
    result = await tool_edit({"path": str(f), "old_string": "x", "new_string": "y", "replace_all": True}, cwd=tmp_path)
    assert result["ok"] and result["replacements"] == 3


async def test_read_keeps_trailing_whitespace_and_numbers_stably(tmp_path: Path):
    f = tmp_path / "w.txt"
    f.write_text("a   \n\tb\t\n" + "c\n" * 998)  # 1000 lines: the number width stays 6 columns
    result = await tool_read({"path": str(f)}, cwd=tmp_path)
    text = format_tool_result(result)
    lines = text.split("\n")
    assert lines[0] == "     1\ta   " and lines[1] == "     2\t\tb\t"  # nothing stripped from the line itself
    assert lines[999] == "  1000\tc"
    # what read shows, minus the prefix, is what edit matches exactly
    old = "\n".join(ln.split("\t", 1)[1] for ln in lines[:2])
    result = await tool_edit({"path": str(f), "old_string": old, "new_string": "z"}, cwd=tmp_path)
    assert result["strategy"] == "exact"
