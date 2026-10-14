"""The claude-cli provider finds the `<tool_calls>` block by its JSON, not by the first closing tag: a file that talks
about the protocol (k3code's own claude_cli.py, its tests, its docs) used to lose the call and cost a model step."""

from __future__ import annotations

import json
from typing import Any

import pytest

from k3code.providers.base import ProviderError
from k3code.providers.claude_cli import _mirrors_reply, _reply_from_result, _StreamGate, _tool_blocks
from k3code.providers.types import INVALID_TOOL_CALL, ToolSpec

TOOLS = [ToolSpec(name="write", description="Write a file", parameters={"type": "object"})]
CONTENT = "s = '</tool_calls>'\nprint(1)"


def _block(path: str = "/x.py", content: str = CONTENT) -> str:
    call = {"name": "write", "arguments": {"path": path, "content": content}}
    return "<tool_calls>" + json.dumps([call]) + "</tool_calls>"


def _reply(raw: str) -> tuple[str, list[Any]]:
    return _reply_from_result({"result": raw}, TOOLS)


def test_a_string_holding_the_closing_tag_does_not_end_the_block() -> None:
    raw = "I will write it.\n" + _block()
    text, calls = _reply(raw)
    assert text == "I will write it."
    assert [(c.name, c.arguments) for c in calls] == [("write", {"path": "/x.py", "content": CONTENT})]
    assert _mirrors_reply({"result": raw}, TOOLS)  # the process may be reused: its history is what k3code keeps


def test_a_fenced_block_holding_the_closing_tag_is_found_too() -> None:
    call = json.dumps([{"name": "write", "arguments": {"path": "/x.py", "content": CONTENT}}])
    _, calls = _reply(f"<tool_calls>\n```json\n{call}\n```\n</tool_calls>")
    assert [c.arguments["content"] for c in calls] == [CONTENT]


def test_every_block_still_counts_when_one_holds_the_closing_tag() -> None:
    _, calls = _reply(f"first\n{_block('/a.py')}\nthen\n{_block('/b.py', 'x')}")
    assert [c.arguments["path"] for c in calls] == ["/a.py", "/b.py"]


def test_a_prose_mention_of_the_opening_tag_does_not_start_the_block() -> None:
    raw = "The protocol uses a <tool_calls> block, I will now call it.\n" + _block("/a", "x")
    text, calls = _reply(raw)
    assert text == "The protocol uses a <tool_calls> block, I will now call it."
    assert [(c.name, c.arguments) for c in calls] == [("write", {"path": "/a", "content": "x"})]
    assert _mirrors_reply({"result": raw}, TOOLS)


def test_the_streamed_text_still_matches_the_stored_text_after_a_prose_mention() -> None:
    raw = "The protocol uses a <tool_calls> block, I will now call it.\n" + _block("/a", "x")
    text, _ = _reply(raw)
    gate = _StreamGate(hold_tool_block=True)
    streamed = "".join(gate.feed(raw[i : i + 3]) for i in range(0, len(raw), 3))
    assert streamed + gate.rest(text) == text


def test_json_that_does_not_decode_is_still_reported_not_lost() -> None:
    (call,) = _reply("<tool_calls>[{not json</tool_calls>")[1]
    assert call.name == INVALID_TOOL_CALL
    _, calls = _reply('<tool_calls>[{"name": "write", "arguments": {"path": "/a",}},]</tool_calls>')
    assert [c.name for c in calls] == ["write"]  # a trailing comma is still repaired


def test_text_between_the_json_and_the_closing_tag_is_still_an_invalid_call() -> None:
    (call,) = _reply('<tool_calls>[{"name": "write", "arguments": {}}] and so on</tool_calls>')[1]
    assert call.name == INVALID_TOOL_CALL


def test_a_prose_pair_of_tags_without_a_real_block_stays_an_invalid_call_not_an_error() -> None:
    text, calls = _reply("Calls go in <tool_calls>JSON array</tool_calls> blocks.")
    assert [c.name for c in calls] == [INVALID_TOOL_CALL]
    assert text == "Calls go in"


def test_a_cut_off_block_after_a_prose_mention_is_still_an_error() -> None:
    with pytest.raises(ProviderError):
        _reply('About the <tool_calls> block.\n<tool_calls>[{"name": "write", "arguments": {"path": "/a')


def test_empty_blocks_are_cut_out_of_the_text_even_next_to_a_prose_mention() -> None:
    text, calls = _reply("a <tool_calls> tag, <tool_calls>[]</tool_calls> b")
    assert calls == [] and text == "a <tool_calls> tag,  b"


def test_block_spans_cover_the_tags() -> None:
    raw = "x " + _block()
    (b,) = _tool_blocks(raw)
    assert raw[b.start : b.end] == _block() and raw[b.end :] == ""
