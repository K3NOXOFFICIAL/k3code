"""Tool calls and tool results must pair up on every request (HTTP 400 otherwise).

Regression: ``LiveSession.history`` rebuilt stored messages without ``tool_calls``, so turn 2 of any session that had
used a tool sent a ``tool`` result with no assistant call in front of it. The fake and claude-cli providers hid it.
"""

from __future__ import annotations

from pathlib import Path

from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer, _serialize_messages
from k3code.gateway.sessions import SessionStore
from k3code.providers.types import (
    Message,
    ToolCall,
    messages_to_anthropic,
    messages_to_openai,
    normalize_tool_pairs,
)


def call(i: str, name: str = "bash") -> ToolCall:
    return ToolCall(id=i, name=name, arguments={"command": "ls"})


def tool(i: str, text: str = "ok") -> Message:
    return Message(role="tool", content=text, tool_call_id=i, name="bash")


def assert_openai_valid(wire: list[dict]) -> None:
    """What chat-completions enforces: every tool message answers a call made by the nearest assistant before it."""
    open_calls: set[str] = set()
    for m in wire:
        if m["role"] == "assistant":
            assert not open_calls, f"calls never answered: {open_calls}"
            open_calls = {tc["id"] for tc in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            assert m["tool_call_id"] in open_calls, f"orphan tool message {m['tool_call_id']}"
            open_calls.discard(m["tool_call_id"])
        elif m["role"] == "user":
            assert not open_calls, f"calls never answered: {open_calls}"
    assert not open_calls, f"calls never answered: {open_calls}"


def test_well_formed_pairs_pass_through_unchanged():
    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", content=None, tool_calls=[call("a"), call("b")]),
        tool("a"),
        tool("b"),
        Message(role="assistant", content="done"),
    ]
    assert normalize_tool_pairs(msgs) == msgs


def test_orphan_tool_result_is_dropped():
    msgs = [Message(role="user", content="go"), tool("zz"), Message(role="assistant", content="done")]
    assert [m.role for m in normalize_tool_pairs(msgs)] == ["user", "assistant"]


def test_unanswered_call_is_dropped_but_text_is_kept():
    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", content="let me look", tool_calls=[call("a"), call("b")]),
        tool("a"),
        Message(role="user", content="next"),
    ]
    out = normalize_tool_pairs(msgs)
    assert [tc.id for tc in out[1].tool_calls] == ["a"]
    assert [m.role for m in out] == ["user", "assistant", "tool", "user"]
    assert [tc.id for tc in msgs[1].tool_calls] == ["a", "b"]  # input untouched


def test_assistant_with_only_unanswered_calls_vanishes():
    msgs = [Message(role="user", content="go"), Message(role="assistant", content=None, tool_calls=[call("a")]),
            Message(role="user", content="next")]
    assert [m.role for m in normalize_tool_pairs(msgs)] == ["user", "user"]


def test_both_wire_formats_are_repaired():
    broken = [
        Message(role="user", content="go"),
        tool("gone"),
        Message(role="assistant", content=None, tool_calls=[call("a")]),
        tool("a"),
        Message(role="assistant", content="done"),
    ]
    assert_openai_valid(messages_to_openai(broken))
    _, rest = messages_to_anthropic(broken)
    assert [m["role"] for m in rest] == ["user", "assistant", "user", "assistant"]
    blocks = [b for m in rest if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"]]
    ids = [b["tool_use_id"] for b in blocks]
    assert ids == ["a"]


def test_session_history_round_trip_keeps_tool_calls(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    srv = GatewayServer(config=Settings(providers=[prov]), store=SessionStore(tmp_path / "s.db"))
    stored = srv.store.create(title="x", model="default", cwd=str(tmp_path))
    live = srv.live_for(stored)
    turn1 = [
        Message(role="system", content="s"),
        Message(role="user", content="run ls"),
        Message(role="assistant", content=None, tool_calls=[call("c1"), call("c2")]),
        tool("c1", "a.txt"),
        tool("c2", "b.txt"),
        Message(role="assistant", content="two files"),
    ]
    stored.messages = _serialize_messages(turn1)
    history = live.history
    assert [tc.id for tc in history[1].tool_calls] == ["c1", "c2"]
    assert history[1].tool_calls[0].arguments == {"command": "ls"}
    # turn 2 request: what the provider is sent
    wire = messages_to_openai([Message(role="system", content="s"), *history, Message(role="user", content="and now?")])
    assert_openai_valid(wire)
    assert any(m.get("tool_calls") for m in wire)
