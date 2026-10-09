"""read: numbered lines, offset/limit, its own char budget and a continuation hint; other tools say how to see more."""

from __future__ import annotations

from k3code.providers.types import Message
from k3code.tools import (
    READ_MAX_CHARS,
    READ_MAX_LINE_CHARS,
    clip_tool_results,
    format_tool_result,
    tool_read,
)


async def read_text(path, **args) -> str:
    return format_tool_result(await tool_read({"path": str(path), **args}))


async def test_read_numbers_lines_like_cat_n(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    assert await read_text(f) == "     1\talpha\n     2\tbeta\n     3\tgamma"


async def test_offset_and_limit_select_lines_and_the_hint_says_how_to_continue(tmp_path):
    f = tmp_path / "long.txt"
    f.write_text("".join(f"row {i}\n" for i in range(1, 401)))
    text = await read_text(f, offset=101, limit=50)
    lines = text.split("\n")
    assert lines[0] == "   101\trow 101"
    assert lines[49] == "   150\trow 150"
    assert lines[-1] == "[file has 400 lines; showed 101-150; continue with offset=151]"
    tail = await read_text(f, offset=351)
    assert tail.split("\n")[-1] == "   400\trow 400"  # the end of the file: no hint
    assert "continue with" not in tail


async def test_default_limit_is_2000_lines(tmp_path):
    f = tmp_path / "many.txt"
    f.write_text("".join(f"{i}\n" for i in range(1, 2501)))
    text = await read_text(f)
    assert "  2000\t2000\n[file has 2500 lines; showed 1-2000; continue with offset=2001]" in text
    assert "\t2001\n" not in text


async def test_a_400_line_file_reaches_the_model_whole_not_head_and_tail(tmp_path):
    f = tmp_path / "mid.py"
    f.write_text("".join(f"line {i:04d} {'x' * 60}\n" for i in range(1, 401)))  # ~29 kB, over the 10k tool clip
    text = await read_text(f)
    sent = clip_tool_results([Message(role="tool", content=text, tool_call_id="c", name="read")])[0].content
    assert sent == text  # read has its own budget: the middle of the file is not cut out
    assert "line 0200" in sent


async def test_read_stops_at_its_char_budget_and_long_lines_are_cut(tmp_path):
    f = tmp_path / "wide.txt"
    f.write_text("".join(f"{i:05d}" + "w" * 1500 + "\n" for i in range(1, 201)))  # ~300 kB
    text = await read_text(f)
    assert len(text) <= READ_MAX_CHARS + 200
    last = text.split("\n")[-1]
    assert last.startswith("[file has 200 lines; showed 1-") and "continue with offset=" in last
    g = tmp_path / "oneline.txt"
    g.write_text("z" * (READ_MAX_LINE_CHARS + 500) + "\n")
    cut = await read_text(g)
    assert f"[line cut: {READ_MAX_LINE_CHARS + 500} chars]" in cut
    assert len(cut) < READ_MAX_LINE_CHARS + 100


async def test_start_and_end_still_work(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("a\nb\nc\nd\n")
    assert (
        await read_text(f, start=2, end=3)
        == "     2\tb\n     3\tc\n[file has 4 lines; showed 2-3; continue with offset=4]"
    )


def test_other_tools_keep_clipping_and_the_marker_says_how_to_get_the_rest():
    big = "m" * 30_000
    msgs = [
        Message(role="tool", content=big, tool_call_id="g", name="grep"),
        Message(role="tool", content=big, tool_call_id="b", name="bash"),
    ]
    grep_sent, bash_sent = (m.content for m in clip_tool_results(msgs))
    assert len(grep_sent) < 10_300 and "grep a narrower pattern" in grep_sent
    assert len(bash_sent) < 10_300 and "head, tail or sed -n" in bash_sent
