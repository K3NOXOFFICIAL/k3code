"""The Anthropic wire: blank text, empty results, per-model effort and sampling, foreign ids, one results message."""

from __future__ import annotations

import logging

import pytest

from k3code.providers import effort
from k3code.providers.anthropic import AnthropicProvider, _add_cache_breakpoints
from k3code.providers.types import (
    Message,
    ToolCall,
    ToolSpec,
    anthropic_tool_id,
    messages_to_anthropic,
    with_cache_breakpoint,
)
from k3code.tools import _format_tool_result

_TOOL = ToolSpec(name="t", description="d", parameters={"type": "object"})


def _call(i: str) -> ToolCall:
    return ToolCall(id=i, name="read", arguments={"path": "a"})


def _payload(messages: list[Message], model: str = "claude-sonnet-4-6", temperature: float | None = None) -> dict:
    p = AnthropicProvider(name="a", api_key="k")
    return p._payload(messages, [_TOOL], model, max_tokens=10, temperature=temperature)


def _marked(wire: list[dict]) -> list[tuple[int, int]]:
    """(message, block) of every cache breakpoint in the messages."""
    return [
        (i, j)
        for i, m in enumerate(wire)
        for j, b in enumerate(m["content"] if isinstance(m["content"], list) else [])
        if "cache_control" in b
    ]


# 1. whitespace-only text blocks


def test_whitespace_assistant_text_is_not_sent_with_its_tool_call() -> None:
    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", content="\n\n", tool_calls=[_call("c1")]),
        Message(role="tool", tool_call_id="c1", content="out"),
    ]
    _, wire = messages_to_anthropic(msgs)
    assert [b["type"] for b in wire[1]["content"]] == ["tool_use"]


def test_blank_turns_are_not_sent_and_never_carry_a_breakpoint() -> None:
    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", content="  "),
        Message(role="user", content="  \n"),
    ]
    _, wire = messages_to_anthropic(msgs)
    assert wire == [{"role": "user", "content": "go"}]
    assert with_cache_breakpoint("  \n") == "  \n"
    assert with_cache_breakpoint([{"type": "text", "text": " "}]) == [{"type": "text", "text": " "}]
    payload = _payload(msgs)
    assert [(m["role"], m["content"][0]["text"]) for m in payload["messages"]] == [("user", "go")]
    assert _marked(payload["messages"]) == [(0, 0)]


def test_a_blank_user_message_keeps_the_reminder_that_follows_it_and_the_tool_pairing() -> None:
    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", content="", tool_calls=[_call("c1")]),
        Message(role="tool", tool_call_id="c1", content="out"),
        Message(role="user", content=" "),
        Message(role="system", content="note"),
    ]
    _, wire = messages_to_anthropic(msgs)
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert [b["type"] for b in wire[2]["content"]] == ["tool_result", "text"]


# 2. an empty last tool result


def test_an_empty_tool_result_reads_no_output() -> None:
    assert _format_tool_result({"content": ""}) == "(no output)"
    assert _format_tool_result({"content": " \n"}) == "(no output)"
    assert _format_tool_result({"content": "text"}) == "text"


def test_the_breakpoint_walks_back_past_an_empty_last_tool_result() -> None:
    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", content="", tool_calls=[_call("c1"), _call("c2")]),
        Message(role="tool", tool_call_id="c1", content="one"),
        Message(role="tool", tool_call_id="c2", content=""),
    ]
    wire = _payload(msgs)["messages"]
    assert _marked(wire) == [(2, 0)]  # the block before the empty one, in the same message
    blocks = [{"type": "tool_result", "tool_use_id": "x", "content": ""}]
    only = [{"role": "user", "content": "q"}, {"role": "user", "content": blocks}]
    _add_cache_breakpoints({"messages": only, "tools": [{"name": "t"}]})
    assert only[0]["content"][0]["cache_control"] == {"type": "ephemeral"}  # ... else in the message before


# 3. effort per model


@pytest.mark.parametrize(
    ("model", "asked", "sent"),
    [
        ("claude-opus-4-5", "xhigh", "high"),
        ("claude-opus-4-5", "max", "high"),
        ("claude-opus-4-5-20251101", "max", "high"),
        ("claude-opus-4-6", "xhigh", "max"),
        ("claude-opus-4-6", "max", "max"),
        ("claude-sonnet-4-6", "xhigh", "max"),
        ("claude-sonnet-4-6", "high", "high"),
        ("claude-opus-4.7", "xhigh", "xhigh"),
        ("claude-opus-4.6", "xhigh", "max"),
        ("claude-opus-4-7", "max", "max"),
        ("claude-opus-5-5", "xhigh", "xhigh"),
        ("claude-fable-5-1", "max", "max"),
        ("claude-haiku-4-5", "xhigh", None),
    ],
)
def test_effort_is_clamped_to_the_levels_the_model_has(model: str, asked: str, sent: str | None) -> None:
    token = effort.REASONING_EFFORT.set(asked)
    try:
        assert effort.anthropic_effort(model) == sent
    finally:
        effort.REASONING_EFFORT.reset(token)


# 4. temperature


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-opus-4-8",
        "claude-fable-5-1",
        "claude-opus-4.7",
    ],
)
def test_temperature_is_left_out_for_models_that_refuse_it(model: str, caplog: pytest.LogCaptureFixture) -> None:
    effort._WARNED_TEMPERATURE.discard(model)
    with caplog.at_level(logging.WARNING, logger=effort.logger.name):
        assert "temperature" not in _payload([Message(role="user", content="hi")], model, temperature=0.2)
        _payload([Message(role="user", content="hi")], model, temperature=0.2)
    assert len([r for r in caplog.records if model in r.getMessage()]) == 1  # warned once


@pytest.mark.parametrize(
    "model", ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5", "claude-3-5-sonnet-latest"]
)
def test_temperature_is_kept_for_the_others(model: str) -> None:
    assert _payload([Message(role="user", content="hi")], model, temperature=0.2)["temperature"] == 0.2


# 5. foreign tool-call ids


def test_foreign_tool_call_ids_are_made_valid_the_same_way_in_the_call_and_its_result() -> None:
    ids = ["functions.read:0", "call_9Zx.1", "x" * 100, "toolu_01ABC-ok_1"]
    msgs = [Message(role="user", content="go"), Message(role="assistant", tool_calls=[_call(i) for i in ids])]
    msgs += [Message(role="tool", tool_call_id=i, content="r") for i in ids]
    _, wire = messages_to_anthropic(msgs)
    used = [b["id"] for b in wire[1]["content"]]
    answered = [b["tool_use_id"] for b in wire[2]["content"]]
    assert used == answered
    assert all(0 < len(i) <= 64 and i.replace("_", "").replace("-", "").isalnum() for i in used)
    assert used[3] == ids[3]  # an id that fits is not touched
    assert len(set(used)) == 4
    assert anthropic_tool_id("a.b") != anthropic_tool_id("a:b") != anthropic_tool_id("a_b")
    assert anthropic_tool_id("functions.read:0") == anthropic_tool_id("functions.read:0")


# 6. one user message for the results of a turn


def test_the_results_of_one_assistant_turn_go_in_one_user_message_in_call_order() -> None:
    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", tool_calls=[_call("a"), _call("b"), _call("c")]),
        Message(role="tool", tool_call_id="a", content="1"),
        Message(role="tool", tool_call_id="b", content="2"),
        Message(role="tool", tool_call_id="c", content="3"),
        Message(role="system", content="note"),
        Message(role="assistant", tool_calls=[_call("d")]),
        Message(role="tool", tool_call_id="d", content="4"),
    ]
    _, wire = messages_to_anthropic(msgs)
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "assistant", "user"]
    assert [(b["type"], b.get("tool_use_id")) for b in wire[2]["content"]] == [
        ("tool_result", "a"),
        ("tool_result", "b"),
        ("tool_result", "c"),
        ("text", None),
    ]
    assert wire[4]["content"] == [{"type": "tool_result", "tool_use_id": "d", "content": "4"}]  # single: as before
