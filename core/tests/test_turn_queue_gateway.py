"""Mid-turn input: session.steer, prompt.submit before a turn is streaming, prompts queued behind a job."""

from __future__ import annotations

import asyncio

from k3code.router import Router, build_chain
from test_permissions_gateway import ScriptedProvider, bash, call, make_server


class GatedProvider(ScriptedProvider):
    """ScriptedProvider whose first stream() call waits until ``gate`` is set."""

    def __init__(self, turns) -> None:
        super().__init__(turns)
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        if self.n == 0:
            self.entered.set()
            await self.gate.wait()
        async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
            yield event


def gated_server(tmp_path, monkeypatch, turns):
    server, _ = make_server(tmp_path, [], monkeypatch, mode="yolo")
    provider = GatedProvider(turns)
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    return server, provider


async def test_a_prompt_before_the_turn_streams_is_queued_not_a_second_task(tmp_path, monkeypatch):
    """streaming only turns True after compaction/MCP start; a prompt in that window started a second task and
    overwrote turn_task, so /stop cancelled the wrong one."""
    server, provider = gated_server(tmp_path, monkeypatch, ["one", "two"])
    provider.gate.set()
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await call(server, "prompt.submit", {"text": "first"})
    task = server.session.turn_task
    assert not server.session.streaming  # the task has not run yet
    second = await call(server, "prompt.submit", {"text": "second"})
    assert second["status"] == "queued"
    assert server.session.turn_task is task
    await asyncio.wait_for(task, 20)
    users = [m["content"] for m in server.session.stored.messages if m["role"] == "user"]
    assert users == ["first", "second"]
