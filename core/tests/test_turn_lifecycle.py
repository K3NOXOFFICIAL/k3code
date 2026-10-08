"""Turn lifecycle regressions: how a turn ends, who may evict or start one, what reaches the disk and when."""

from __future__ import annotations

import asyncio

from k3code.automation.server_runner import ServerRunner
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.router import Router, build_chain
from m1cmd_helpers import git_repo, make_server, new_session

NO_ADVISOR = {"advisor_on_goal": False}


class RepeatingTool:
    """Every model call asks for the same `read`: the loop guard notes it, then stops the turn."""

    name = "fake"
    base_url = "fake://"

    def __init__(self) -> None:
        self.n = 0

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.n += 1
        call = ToolCall(id=f"c{self.n}", name="read", arguments={"path": "a.py"})
        yield StreamEvent(type="done", message=Message(role="assistant", content="", tool_calls=[call]), usage=Usage())

    async def aclose(self) -> None:
        return None


def scripted_judge(verdicts):
    calls: list[tuple[str, str]] = []

    async def judge(goal: str, response: str):
        calls.append((goal, response))
        v = verdicts[min(len(calls) - 1, len(verdicts) - 1)]
        return v, f"verdict {v}", False, False

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def _use(server, provider) -> None:
    server.providers = [provider]
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]


async def test_a_loop_guard_stop_pauses_the_goal_instead_of_continuing_it(tmp_path, monkeypatch):
    """The loop-guard stop ended the turn 'done': the judge said continue and the goal re-ran the same tool calls
    turn after turn until the budget ran out."""
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, autonomy=NO_ADVISOR, permission_mode="yolo")
    provider = RepeatingTool()
    _use(server, provider)
    sid = await new_session(server, repo)
    live = server.live[sid]
    mgr = server.goal_manager(live)
    mgr.set("fix the flaky test", max_turns=3)
    server.goal_judge = scripted_judge(["continue"])
    status, _ = await server._run_turn(live, "fix the flaky test")
    assert status == "needs_input"
    assert server.goal_judge.calls == []  # never judged, never continued
    assert mgr.state.status == "paused" and mgr.state.paused_reason == "needs_input"
    assert provider.n <= 5, provider.n  # one guarded turn, not one per goal turn
    await server.close()


async def test_a_loop_guard_stop_reports_a_loop_tick_as_blocked(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, permission_mode="yolo")
    _use(server, RepeatingTool())
    sid = await new_session(server, repo)
    res = await ServerRunner(server).run_prompt("watch the build", session_id=sid, kind="loop_tick")
    assert res.status == "blocked", res
    await server.close()


async def test_closing_a_session_whose_goal_is_being_judged_keeps_it_live(tmp_path, monkeypatch):
    """`streaming` is False during the judge, the check and compaction while the turn task runs on: session.close
    evicted the session and the watchdog then built a second LiveSession and kicked the goal twice."""
    from m1cmd_helpers import rpc

    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, ["working on it"], autonomy=NO_ADVISOR)
    a = await new_session(server, repo)
    live = server.live[a]
    server.goal_manager(live).set("ship it")
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_judge(goal: str, response: str):
        entered.set()
        await release.wait()
        return "done", "shipped", False, False

    server.goal_judge = slow_judge
    live.turn_task = asyncio.create_task(server._run_turn(live, "ship it"))
    await asyncio.wait_for(entered.wait(), 20)
    assert not live.streaming
    await new_session(server, repo)  # the client moves on to another session
    assert (await rpc(server, "session.close", {"session_id": a}))["result"]["closed"] is False
    assert a in server.live
    assert await server.watchdog_tick() == []  # no second driver
    release.set()
    await asyncio.wait_for(live.turn_task, 20)
    assert provider.n == 1
    await server.close()


async def test_releasing_finished_runs_never_evicts_one_whose_turn_is_still_running(tmp_path, monkeypatch):
    from k3code.automation import server_runner

    monkeypatch.setattr(server_runner, "KEEP_FINISHED_RUNS", 0)
    server, _ = make_server(tmp_path, monkeypatch)
    stored = server.store.create(title="auto", cwd=str(tmp_path))
    stored.meta.update({"background": True, "origin": "automation"})
    server.store.save(stored)
    live = server.live_for(stored)
    gate = asyncio.Event()
    live.turn_task = asyncio.create_task(gate.wait())  # e.g. the goal check runs: not streaming, not finished
    await ServerRunner(server)._release(live)
    assert stored.session_id in server.live
    gate.set()
    await live.turn_task
    await ServerRunner(server)._release(live)
    assert stored.session_id not in server.live  # finished now: evictable as before
    await server.close()


class GatedText:
    """Text replies; the first call waits for `release`. Records whether the session ran unattended per call."""

    name = "fake"
    base_url = "fake://"

    def __init__(self, live_ref) -> None:
        self.live_ref = live_ref
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.background: list[bool] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.background.append(bool(self.live_ref().background))
        if len(self.background) == 1:
            self.entered.set()
            await self.release.wait()
        yield StreamEvent(type="text_delta", text="ok")
        yield StreamEvent(type="done", message=Message(role="assistant", content="ok", tool_calls=[]), usage=Usage())

    @property
    def n(self) -> int:
        return len(self.background)

    async def aclose(self) -> None:
        return None


async def test_a_loop_tick_waits_for_a_user_turn_that_is_not_streaming_yet(tmp_path, monkeypatch):
    """run_prompt waited only on `streaming`: in a user turn's pre-streaming window (compaction, MCP start) the tick
    replaced turn_task and flipped background=True under the user's turn, so /stop hit the wrong task."""
    from k3code.automation import server_runner

    monkeypatch.setattr(server_runner, "BUSY_POLL_S", 0.01)
    server, provider = make_server(tmp_path, monkeypatch, ["tick reply"])
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    user_turn_go = asyncio.Event()
    user_turn = asyncio.create_task(user_turn_go.wait())  # the user's turn, still compacting: not streaming
    live.turn_task = user_turn
    run = asyncio.create_task(ServerRunner(server).run_prompt("tick", session_id=sid, kind="loop_tick"))
    await asyncio.sleep(0.2)
    assert live.turn_task is user_turn and live.background is False and provider.n == 0
    user_turn_go.set()
    res = await asyncio.wait_for(run, 20)
    assert res.status == "completed" and provider.n == 1
    await server.close()


async def test_a_prompt_queued_during_a_loop_tick_runs_as_an_attended_turn(tmp_path, monkeypatch):
    """A prompt the user queued behind a tick was drained inside the tick's own turn, with the tick's background flag
    and tools: the user's prompt ran sandboxed, on the cheap tier, as an unattended loop_tick."""
    from m1cmd_helpers import rpc

    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    provider = GatedText(lambda: live)
    _use(server, provider)
    server._tiers = None
    run = asyncio.create_task(ServerRunner(server).run_prompt("tick", session_id=sid, kind="loop_tick"))
    await asyncio.wait_for(provider.entered.wait(), 20)
    queued = (await rpc(server, "prompt.submit", {"text": "my own question"}))["result"]
    assert queued["status"] == "queued"
    provider.release.set()
    res = await asyncio.wait_for(run, 20)
    assert res.status == "completed"
    await asyncio.wait_for(live.turn_task, 20)
    assert provider.background == [True, False]  # the tick unattended, the user's prompt attended
    assert live.background is False
    assert [m["content"] for m in live.stored.messages if m["role"] == "user"] == ["tick", "my own question"]
    await server.close()
