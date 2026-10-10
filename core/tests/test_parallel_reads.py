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
