"""Fake provider ``when: turn_first|turn_after_tool`` is relative to the latest user message."""

from __future__ import annotations

from k3code.providers.fake import FakeProvider
from k3code.providers.types import Message


async def _text(steps, messages) -> tuple[list[str], list[str]]:
    p = FakeProvider(steps=steps)
    text, calls = [], []
    async for ev in p.stream(messages, [], "m"):
        if ev.type == "text_delta":
            text.append(ev.text)
        if ev.type == "tool_call":
            calls.append(ev.tool_call.name)
    return text, calls


async def test_turn_relative_when():
    steps = [
        {"type": "tool_call", "name": "bash", "when": "turn_first", "arguments": {"command": "true"}},
        {"type": "text", "text": "done", "when": "turn_after_tool"},
    ]
    old_tool = [Message(role="user", content="a"), Message(role="tool", content="r", tool_call_id="1")]
    # a tool result exists in history, but not in the current turn -> still "turn_first"
    t, c = await _text(steps, [*old_tool, Message(role="user", content="b")])
    assert c == ["bash"] and t == []
    t, c = await _text(
        steps, [*old_tool, Message(role="user", content="b"), Message(role="tool", content="r", tool_call_id="2")]
    )
    assert c == [] and t == ["done"]
