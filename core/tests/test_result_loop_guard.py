"""E3: the loop guard also watches results (same failure, ABAB alternation) and a main-tier turn stops at 8 errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.agent.loop import AgentLoop
from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec, Usage
from k3code.reliability.loopguard import LoopGuard, Verdict
from k3code.router import Router, build_chain
from k3code.toolerrors import failure_of, normalise
from test_permissions_gateway import call, make_server, run_turn


class _Script:
    """Plays one tool call per model call, then a final text."""

    name = "fake"
    base_url = "https://fake.test"

    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls, self.n = calls, 0
        self.seen: list[list[Message]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.seen.append(list(messages))
        tc = self.calls[self.n] if self.n < len(self.calls) else None
        self.n += 1
        msg = Message(role="assistant", content=None if tc else "done", tool_calls=[tc] if tc else [])
        yield StreamEvent(type="done", message=msg, usage=Usage())

    async def aclose(self) -> None:
        pass


def _loop(tmp: Path, calls: list[ToolCall], **kw) -> tuple[AgentLoop, _Script]:
    provider = _Script(calls)
    loop = AgentLoop(
        Router(build_chain([provider], [["m"]]), max_retries=0),
        system_prompt="t",
        permission_mode="yolo",
        cwd=tmp,
        max_turns=30,
        **kw,
    )

    async def db(arguments, *, cwd=None):
        if arguments.get("ok"):
            return {"content": "rows"}
        return {"error": f"connection refused to 127.0.0.1:{arguments.get('port', 5432)} (attempt {arguments['n']})"}

    loop.tools.register(
        ToolSpec(name="db", description="", parameters={"type": "object", "properties": {"n": {"type": "integer"}}}), db
    )
    return loop, provider


async def _drain(loop: AgentLoop) -> list[StreamEvent]:
    return [e async for e in loop.run("go")]


def _notes(messages: list[Message]) -> list[str]:
    return [m.content or "" for m in messages if m.role == "system"][1:]  # [0] is the system prompt


def test_signature_normalises_paths_numbers_hex_and_uuids():
    a = failure_of(
        "db", {}, {"error": "boom at /srv/app/x.py:12 id 3fa85f64-5717-4562-b3fc-2c963f66afa6 sha deadbeef1"}
    )
    b = failure_of("db", {}, {"error": "boom at src/y.py:7 id 0fa85f64-5717-4562-b3fc-2c963f66afa7 sha cafe0123b"})
    assert a.signature == b.signature == "boom at <path>:<n> id <uuid> sha <hex>"
    bash = failure_of(
        "bash", {"command": "CI=1 npm test -- -x"}, {"stderr": "sh: 1: npm: not found\n", "exit_code": 127}
    )
    assert bash.error_class == "exit 127" and bash.signature == "npm: exit <n>: sh: <n>: npm: not found"
    assert failure_of("bash", {"command": "true"}, {"stdout": "", "exit_code": 0}) is None
    assert len(normalise("x" * 500)) == 160
    leak = normalise("postgresql://app:" + "pa55" + "w0rdXYZ@db/x sk-proj-" + "1234567890" + "abcdefghijkl")
    assert "pa55" not in leak and "1234567890" not in leak and "w0rdXYZ" not in leak


def test_three_identical_failures_with_different_args_remind_once():
    g = LoopGuard()
    out = [g.observe_tool_result("db", {"n": i}, "connection refused", "db") for i in range(5)]
    assert [o.verdict for o in out] == [Verdict.OK, Verdict.OK, Verdict.NOTE, Verdict.OK, Verdict.OK]
    assert "failed the same way" in out[2].note and "connection refused" in out[2].note
    g.reset()
    assert g.observe_tool_result("db", {"n": 9}, "connection refused").verdict is Verdict.OK  # a new turn starts over


def test_success_breaks_the_failure_run():
    g = LoopGuard()
    for i, failure in enumerate(["e", "e", None, "e", "e"]):
        assert g.observe_tool_result("db", {"n": i}, failure).verdict is Verdict.OK


def test_abab_alternation_over_six_calls_is_noted():
    g = LoopGuard()
    calls = [("read", {"path": "a"}), ("read", {"path": "b"})] * 3
    out = [g.observe_tool_result(t, a, None) for t, a in calls]
    assert [o.verdict for o in out[:5]] == [Verdict.OK] * 5
    assert out[5].verdict is Verdict.NOTE and "alternating" in out[5].note
    assert g.observe_tool_result("read", {"path": "a"}, None).verdict is Verdict.OK  # once per pair


@pytest.mark.asyncio
async def test_loop_injects_a_specific_reminder_after_three_identical_failures(tmp_path):
    loop, provider = _loop(tmp_path, [ToolCall(id=f"c{i}", name="db", arguments={"n": i}) for i in range(3)])
    await _drain(loop)
    (note,) = _notes(loop.turn_messages)
    assert "The last 3 tool calls failed the same way" in note and "connection refused to" in note
    # sent with the next request, after the third result (WS4 turns it into a user-side reminder)
    assert any(m.role == "system" and m.content == note for m in provider.seen[3])


@pytest.mark.asyncio
async def test_main_tier_stops_after_eight_errors_listing_them(tmp_path):
    calls = [ToolCall(id=f"c{i}", name="db", arguments={"n": i, **({"port": 1} if i % 2 else {})}) for i in range(12)]
    loop, provider = _loop(tmp_path, calls, max_tool_errors=8)
    events = await _drain(loop)
    assert provider.n == 8 and loop.escalation_reason == "tool_errors"
    final = events[-1].message
    assert final.role == "assistant" and final.content.startswith("I stopped because 8 tool calls in a row failed:")
    assert "- db: connection refused to 127.0.0.1:5432 (attempt 0)" in final.content
    assert final.content.count("\n- db: ") == 8


@pytest.mark.asyncio
async def test_gateway_main_tier_without_escalation_stops_at_eight(tmp_path, monkeypatch):
    """With ``escalate_main`` off the main tier is the last one (by default strong continues: test_autonomy_gateway)."""
    reads = [ToolCall(id=f"r{i}", name="read", arguments={"path": f"missing-{i}.txt"}) for i in range(10)]
    server, provider = make_server(
        tmp_path, [*reads, "never reached"], monkeypatch, mode="yolo", autonomy={"escalate_main": False}
    )
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_turn(server, "go", [])
    msgs = server.session.stored.messages
    assert sum(1 for m in msgs if m["role"] == "tool") == 8
    assert msgs[-1]["role"] == "assistant" and "8 tool calls in a row failed" in msgs[-1]["content"]
    assert server.session.needs_input
