"""Consecutive read-only tool calls of one model reply run concurrently; everything else is a barrier."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from k3code.agent.loop import AgentLoop, ApprovalResult
from k3code.permissions import Decision
from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec, Usage
from k3code.router import Router, build_chain

PARAMS = {"type": "object", "properties": {}}


class ScriptedProvider:
    def __init__(self, replies: list[Message]):
        self._replies = replies
        self._n = 0
        self.name = "fake"
        self.base_url = "https://fake.test"

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        msg = self._replies[self._n] if self._n < len(self._replies) else Message(role="assistant", content="done")
        self._n += 1
        yield StreamEvent(type="done", message=msg, usage=Usage())

    async def aclose(self):
        pass


def make_loop(tmp: Path, replies: list[list[ToolCall]], **kw) -> AgentLoop:
    provider = ScriptedProvider([Message(role="assistant", content=None, tool_calls=c) for c in replies])
    router = Router(build_chain([provider], [["fake-model"]]), max_retries=0)
    kw.setdefault("permission_mode", "yolo")
    return AgentLoop(router, system_prompt="t", max_turns=5, cwd=tmp, **kw)


def register(loop: AgentLoop, name: str, log: list[str], *, side_effect: bool, delay: float = 0.0) -> None:
    async def handler(args, cwd=None):
        log.append(f"start {name}")
        await asyncio.sleep(delay)
        log.append(f"end {name}")
        return {"content": f"{name} ok"}

    loop.tools.register(ToolSpec(name=name, description=name, parameters=PARAMS, side_effect=side_effect), handler)


async def run(loop: AgentLoop) -> list[Message]:
    out = []
    async for event in loop.run("go"):
        if event.type == "done" and event.message and event.message.role == "tool":
            out.append(event.message)
    return out


def calls_for(*names: str) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(names)]


@pytest.mark.asyncio
async def test_independent_reads_run_concurrently_and_report_in_call_order(tmp_path):
    log: list[str] = []
    loop = make_loop(tmp_path, [calls_for("slow_a", "slow_b", "slow_c")])
    for n in ("slow_a", "slow_b", "slow_c"):
        register(loop, n, log, side_effect=False, delay=0.3)
    began = time.monotonic()
    results = await run(loop)
    elapsed = time.monotonic() - began
    assert elapsed < 0.7, elapsed  # sequential would take 0.9 s
    assert [m.tool_call_id for m in results] == ["c0", "c1", "c2"]
    assert log[:3] == ["start slow_a", "start slow_b", "start slow_c"]


@pytest.mark.asyncio
async def test_a_write_between_reads_is_a_barrier(tmp_path):
    log: list[str] = []
    loop = make_loop(tmp_path, [calls_for("r1", "r2", "w", "r3", "r4")])
    for n in ("r1", "r2", "r3", "r4"):
        register(loop, n, log, side_effect=False, delay=0.05)
    register(loop, "w", log, side_effect=True, delay=0.05)
    results = await run(loop)
    assert [m.tool_call_id for m in results] == ["c0", "c1", "c2", "c3", "c4"]
    assert log.index("start w") > log.index("end r1") and log.index("start w") > log.index("end r2")
    assert log.index("start r3") > log.index("end w") and log.index("start r4") > log.index("end w")
    assert log.index("start r2") < log.index("end r1")  # the two reads before the write did overlap


@pytest.mark.asyncio
async def test_a_call_that_would_ask_keeps_its_batch_sequential(tmp_path):
    log: list[str] = []
    asked: list[str] = []
    loop = make_loop(tmp_path, [calls_for("r1", "r2", "r3")], permission_mode="ask")
    for n in ("r1", "r2", "r3"):
        register(loop, n, log, side_effect=False, delay=0.05)

    def decide(tool, args, *, headless=False):
        return Decision(action="ask") if tool == "r2" else Decision(action="allow")

    loop.permissions.decide = decide  # type: ignore[method-assign]

    async def approve(tool, args, decision):
        asked.append(tool)
        assert log[-1].startswith("end")  # nothing else is running while the prompt is up
        return ApprovalResult(choice="once")

    loop.approval_callback = approve
    results = await run(loop)
    assert asked == ["r2"]
    assert log == ["start r1", "end r1", "start r2", "end r2", "start r3", "end r3"]
    assert [m.tool_call_id for m in results] == ["c0", "c1", "c2"]


@pytest.mark.asyncio
async def test_unchanged_read_pointer_names_the_step_of_the_original_read(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\n")
    g = tmp_path / "b.txt"
    g.write_text("world\n")

    def read(call_id: str, path: Path) -> ToolCall:
        return ToolCall(id=call_id, name="read", arguments={"path": str(path)})

    loop = make_loop(tmp_path, [[read("a", f), read("b", g)], [read("a2", f), read("b2", g)]])
    results = {m.tool_call_id: m.content for m in await run(loop)}
    assert results["a2"].startswith("[unchanged since the read at step 1 (call a)")
    assert results["b2"].startswith("[unchanged since the read at step 2 (call b)")


def register_task_like(loop: AgentLoop, log: list[str], delay: float) -> None:
    """A side-effecting tool whose handler says which calls are read-only (what ``task`` does for an explorer)."""

    async def handler(args, cwd=None):
        log.append(f"start {args['who']}")
        await asyncio.sleep(delay)
        log.append(f"end {args['who']}")
        return {"content": args["who"]}

    handler.parallel_when = lambda args: args.get("kind") == "reader"  # type: ignore[attr-defined]
    loop.tools.register(ToolSpec(name="job", description="job", parameters=PARAMS, side_effect=True), handler)


def job(call_id: str, who: str, kind: str) -> ToolCall:
    return ToolCall(id=call_id, name="job", arguments={"who": who, "kind": kind})


@pytest.mark.asyncio
async def test_calls_a_side_effect_tool_marks_read_only_overlap_and_the_others_do_not(tmp_path):
    log: list[str] = []
    loop = make_loop(
        tmp_path,
        [[job("c0", "r1", "reader"), job("c1", "r2", "reader"), job("c2", "w", "writer"), job("c3", "r3", "reader")]],
    )
    register_task_like(loop, log, 0.1)
    results = await run(loop)
    assert [m.tool_call_id for m in results] == ["c0", "c1", "c2", "c3"]
    assert log.index("start r2") < log.index("end r1")  # the two readers overlapped
    assert log.index("start w") > log.index("end r2")  # the writer waited for them ...
    assert log.index("start r3") > log.index("end w")  # ... and the next reader waited for the writer


@pytest.mark.asyncio
async def test_three_explorer_calls_in_one_reply_take_the_time_of_one(tmp_path):
    log: list[str] = []
    loop = make_loop(tmp_path, [[job(f"c{i}", f"e{i}", "reader") for i in range(3)]])
    register_task_like(loop, log, 0.4)
    began = time.monotonic()
    await run(loop)
    assert time.monotonic() - began < 0.9  # in a row: 1.2 s
    assert log[:3] == ["start e0", "start e1", "start e2"]


def register_limited(loop: AgentLoop, log: list[str], limit: int, delay: float = 0.1) -> list[int]:
    """A `job` tool whose handler allows ``limit`` calls together; returns the peak concurrency as it runs."""
    peak = [0]
    live = [0]

    async def handler(args, cwd=None):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        log.append(f"start {args['who']}")
        await asyncio.sleep(delay)
        live[0] -= 1
        return {"content": args["who"]}

    handler.parallel_when = lambda args: True  # type: ignore[attr-defined]
    handler.parallel_limit = lambda: limit  # type: ignore[attr-defined]
    loop.tools.register(ToolSpec(name="job", description="job", parameters=PARAMS, side_effect=True), handler)
    return peak


@pytest.mark.asyncio
async def test_a_long_run_of_parallel_calls_is_cut_to_what_the_tool_allows_and_none_is_refused(tmp_path):
    log: list[str] = []
    loop = make_loop(tmp_path, [[job(f"c{i}", f"e{i}", "reader") for i in range(6)]])
    peak = register_limited(loop, log, limit=4)
    results = await run(loop)
    assert [m.tool_call_id for m in results] == [f"c{i}" for i in range(6)]  # all six ran, in call order
    assert peak[0] == 4  # four together, then the other two


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [["explorer"], {"x": 1}, 7, ""])
async def test_a_predicate_that_gets_odd_arguments_never_breaks_the_turn(tmp_path, bad):
    """`agent_type` as a list raised a TypeError out of loop.run before the call could be answered."""
    loop = make_loop(
        tmp_path, [[ToolCall(id="c0", name="job", arguments={"who": "a", "agent_type": bad}), job("c1", "b", "reader")]]
    )
    seen: list[object] = []

    async def handler(args, cwd=None):
        return {"content": "ok"}

    def when(args):
        seen.append(args.get("agent_type"))
        return {}[args["agent_type"]] if isinstance(args.get("agent_type"), list) else True  # raises for a list

    handler.parallel_when = when  # type: ignore[attr-defined]
    loop.tools.register(ToolSpec(name="job", description="job", parameters=PARAMS, side_effect=True), handler)
    results = await run(loop)
    assert [m.tool_call_id for m in results] == ["c0", "c1"]


class _FakeMcp:
    """Just enough of McpManager for register_mcp_tools: two servers, one declared read-only."""

    def __init__(self) -> None:
        from k3code.config import McpServerConfig
        from k3code.mcpclient import McpToolInfo

        self.servers = {"ro": McpServerConfig(command="x", parallel_safe=True), "rw": McpServerConfig(command="x")}
        self._tools = [McpToolInfo(s, "look", "d", {"type": "object", "properties": {}}) for s in ("ro", "rw")]
        self.live = {"ro": 0, "rw": 0}
        self.peak = {"ro": 0, "rw": 0}

    def tools(self):
        return self._tools

    async def call(self, qualified: str, args: dict) -> dict:
        server = qualified.split("__")[1]
        self.live[server] += 1
        self.peak[server] = max(self.peak[server], self.live[server])
        await asyncio.sleep(0.05)
        self.live[server] -= 1
        return {"content": qualified}


@pytest.mark.asyncio
async def test_calls_to_a_parallel_safe_mcp_server_overlap_and_those_to_others_do_not(tmp_path):
    from k3code.extratools import register_mcp_tools

    mcp = _FakeMcp()
    calls = [ToolCall(id=f"{s}{i}", name=f"mcp__{s}__look", arguments={}) for s in ("ro", "rw") for i in range(3)]
    loop = make_loop(tmp_path, [calls])
    register_mcp_tools(loop.tools, mcp)
    loop.tools.activate(["mcp__ro__look", "mcp__rw__look"])
    results = await run(loop)
    assert [m.tool_call_id for m in results] == [c.id for c in calls]  # in call order, all answered
    assert mcp.peak["ro"] == 3  # the read-only server's three calls ran together
    assert mcp.peak["rw"] == 1  # an ordinary server's did not
