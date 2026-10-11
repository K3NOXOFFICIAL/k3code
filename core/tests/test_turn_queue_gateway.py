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


async def test_steer_reaches_the_running_loop_and_survives_the_persist(tmp_path, monkeypatch):
    """session.steer appended to stored.messages: the running loop never saw it and the turn's persist overwrote it,
    while the client was told steered: true."""
    server, provider = gated_server(tmp_path, monkeypatch, [bash("echo hi"), "ok"])
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await call(server, "prompt.submit", {"text": "do it"})
    await asyncio.wait_for(provider.entered.wait(), 5)
    reply = await call(server, "session.steer", {"text": "also say bye"})
    # the TUI treats any reply without status "queued" as rejected and queues the text again for the next turn: a
    # successful steer used to reach the model twice
    assert reply["steered"] is True and reply["status"] == "queued"
    provider.gate.set()
    await asyncio.wait_for(server.session.turn_task, 20)
    second = provider.seen[1]
    i = next(i for i, m in enumerate(second) if m.role == "user" and m.content == "also say bye")
    assert second[i - 1].role == "tool"  # after the tool result, never between tool_calls and results
    users = [m["content"] for m in server.session.stored.messages if m["role"] == "user"]
    assert users == ["do it", "also say bye"]


async def test_steer_after_the_final_answer_is_answered_in_the_same_turn(tmp_path, monkeypatch):
    server, provider = gated_server(tmp_path, monkeypatch, ["first answer", "second answer"])
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await call(server, "prompt.submit", {"text": "q"})
    await asyncio.wait_for(provider.entered.wait(), 5)
    await call(server, "session.steer", {"text": "and another thing"})
    provider.gate.set()
    await asyncio.wait_for(server.session.turn_task, 20)
    rows = [(m["role"], m["content"]) for m in server.session.stored.messages]
    assert rows[-2:] == [("user", "and another thing"), ("assistant", "second answer")]


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


async def test_prompts_queued_behind_a_job_run_when_it_ends(tmp_path, monkeypatch):
    """/ultraplan etc. run through start_job; prompts typed meanwhile were queued and never run."""
    server, provider = gated_server(tmp_path, monkeypatch, ["answer"])
    provider.gate.set()
    await call(server, "session.create", {"cwd": str(tmp_path)})
    live = server.session
    release = asyncio.Event()

    async def job() -> str:
        await release.wait()
        return "plan ready"

    server.start_job(live, "/ultraplan x", job)
    await asyncio.sleep(0.05)
    assert (await call(server, "prompt.submit", {"text": "after the job"}))["status"] == "queued"
    release.set()
    await asyncio.wait_for(live.turn_task, 20)
    rows = [(m["role"], m["content"]) for m in live.stored.messages]
    assert rows[-2:] == [("user", "after the job"), ("assistant", "answer")]
    assert live.pending_prompts == []


async def test_transcript_edits_are_refused_or_steered_mid_turn(tmp_path, monkeypatch):
    """/clear, /compact and /advisor accept rewrote stored.messages while the turn's persist overwrote them."""
    server, _ = gated_server(tmp_path, monkeypatch, [])
    await call(server, "session.create", {"cwd": str(tmp_path)})
    live = server.session
    live.messages = [{"role": "user", "content": "keep"}]
    live.streaming = True
    try:
        out = await call(server, "command.dispatch", {"name": "clear", "arg": ""})
        assert "running" in out["message"] and live.messages == [{"role": "user", "content": "keep"}]
        out = await call(server, "command.dispatch", {"name": "compact", "arg": ""})
        assert "running" in out["message"]
        out = await call(server, "command.dispatch", {"name": "preview", "arg": "a task"})
        assert "running" in out["message"]
        live.pending_advisor = "Risk: none."
        out = await call(server, "command.dispatch", {"name": "advisor", "arg": "accept"})
        assert live.steer_queue == ["Advisor review:\nRisk: none."]
        assert live.messages == [{"role": "user", "content": "keep"}]
    finally:
        live.streaming = False
