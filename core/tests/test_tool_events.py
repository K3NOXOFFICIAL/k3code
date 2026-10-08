"""tool.start / tool usage rows / subagent tool counts for providers that only attach calls to the done message."""

from __future__ import annotations

import pytest

from k3code.providers.fake import FakeProvider
from test_autonomy_gateway import events, make, run_turn, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}


@pytest.fixture(params=["streamed", "done_only"])
def tool_call_events(request, monkeypatch):
    """``done_only``: drop tool_call stream events, like openai_compat and anthropic (calls ride on ``done``)."""
    if request.param == "done_only":
        real = FakeProvider.stream

        async def stream(self, *a, **k):
            async for ev in real(self, *a, **k):
                if ev.type != "tool_call":
                    yield ev

        monkeypatch.setattr(FakeProvider, "stream", stream)
    return request.param


async def test_every_tool_call_is_announced_and_counted_once(tmp_path, monkeypatch, tool_call_events):
    steps = [
        {"type": "tool_call", "when": "first", "name": "bash", "arguments": {"command": "echo hi"}},
        {"type": "text", "when": "after_tool", "text": "done"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="yolo", **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "run it")
    starts = events(server, "tool.start")
    assert [s["name"] for s in starts] == ["bash"]
    assert [c["tool_id"] for c in events(server, "tool.complete")] == [starts[0]["tool_id"]]
    rows = [r for r in server.usage.rows() if r["kind"] == "tool"]
    assert [r["detail"] for r in rows] == ["bash"]


async def test_subagent_tool_count_without_tool_call_events(tmp_path, monkeypatch, tool_call_events):
    steps = [
        {
            "type": "tool_call",
            "match": "PARENT",
            "when": "first",
            "name": "task",
            "arguments": {"description": "child job", "prompt": "CHILD look", "agent_type": "explorer"},
        },
        {"type": "text", "match": "PARENT", "when": "after_tool", "text": "parent done"},
        {
            "type": "tool_call",
            "match": "[agent:explorer]",
            "when": "turn_first",
            "name": "glob",
            "arguments": {"pattern": "*.md"},
        },
        {"type": "text", "match": "[agent:explorer]", "when": "turn_after_tool", "text": "explored"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="yolo", **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "PARENT: explore")
    (h,) = server.subagents.for_session(server.session.session_id)
    assert h.tool_count == 1 and h.last_tool == "glob"
    assert [e["tool_name"] for e in events(server, "subagent.tool")] == ["glob"]
