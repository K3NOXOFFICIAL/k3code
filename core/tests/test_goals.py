"""Goal lifecycle across the daemon: boot resume (P1-1), graceful stop, kicks and the halt interplay."""

from __future__ import annotations

import asyncio

from k3code.agent.loop import AgentLoop
from k3code.automation.server_runner import ServerRunner
from k3code.errors import ChainExhausted
from k3code.gateway.sessions import SessionStore
from k3code.goals import GoalManager, GoalState
from k3code.reliability import BudgetExceeded
from k3code.reliability.persistent_retry import TurnCancelled
from m1cmd_helpers import git_repo, make_server, new_session

NO_ADVISOR = {"advisor_on_goal": False}


def scripted_judge(verdicts):
    calls: list[tuple[str, str]] = []

    async def judge(goal: str, response: str):
        calls.append((goal, response))
        v = verdicts[min(len(calls) - 1, len(verdicts) - 1)]
        return v, f"verdict {v}", False, False

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def _goal(server, sid) -> GoalManager:
    return server.goal_manager(server.live_for(server.store.get(sid)))


def _persisted_goal(tmp_path, sid) -> GoalState | None:
    """The goal as the next boot would read it (a fresh handle: the server that wrote it is closed)."""
    stored = SessionStore(tmp_path / "sessions.db").get(sid)
    return GoalState.from_dict(stored.meta["goal"]) if stored and stored.meta.get("goal") else None


async def test_boot_resumes_an_active_goal_with_exactly_one_judged_turn(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    first, _ = make_server(tmp_path, monkeypatch, replies=["working"], autonomy=NO_ADVISOR)
    sid = await new_session(first, repo)
    _goal(first, sid).set("ship the feature")
    await first.close()  # the daemon goes away with the goal still active

    second, provider = make_server(tmp_path, monkeypatch, replies=["the feature is shipped"], autonomy=NO_ADVISOR)
    judge = scripted_judge(["done"])
    second.goal_judge = judge
    assert await second.resume_goals() == 1
    await asyncio.wait_for(second.live[sid].turn_task, 20)
    assert len(judge.calls) == 1 and provider.n == 1
    assert _goal(second, sid).state.status == "done"
    await second.close()


async def test_graceful_stop_pauses_with_daemon_restart_and_next_boot_resumes(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    first, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    sid = await new_session(first, repo)
    _goal(first, sid).set("long task")
    live = first.live_for(first.store.get(sid))

    async def hang(session, text):  # a turn that is still running when the daemon stops
        await asyncio.Event().wait()
        return "done", ""

    first._run_one_turn = hang  # type: ignore[method-assign]
    live.turn_task = asyncio.get_running_loop().create_task(first._run_turn(live, "go"))
    await asyncio.sleep(0.05)
    await first.close()
    state = _persisted_goal(tmp_path, sid)
    assert state.status == "paused" and state.paused_reason == "daemon restart"

    second, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    second.goal_judge = scripted_judge(["continue"])
    assert await second.resume_goals() == 1  # the paused-by-restart goal comes back
    assert _goal(second, sid).state.status == "active"
    second.live[sid].turn_task.cancel()
    await asyncio.gather(second.live[sid].turn_task, return_exceptions=True)
    await second.close()


async def test_boot_with_halt_set_runs_zero_turns(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    first, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    sid = await new_session(first, repo)
    _goal(first, sid).set("held task")
    await first.halt_daemon("maintenance")
    await first.close()

    second, provider = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    assert await second.resume_goals() == 0
    assert provider.n == 0
    await second.close()


def _notices(server, key_prefix: str) -> list[dict]:
    shown = [e["payload"] for e in server.event_log if e["type"] == "notification.show"]
    return [p for p in shown if str(p.get("key", "")).startswith(key_prefix)]


async def test_budget_exceeded_pauses_goal_as_needs_input_and_the_run_is_blocked(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    sid = await new_session(server, repo)
    _goal(server, sid).set("spend the budget")

    async def over_budget(self, *args, **kwargs):
        raise BudgetExceeded("session token budget exceeded")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(AgentLoop, "run", over_budget)
    result = await ServerRunner(server).run_prompt("go", session_id=sid)
    assert result.status == "blocked"  # not "completed", not a silent stop
    state = _goal(server, sid).state
    assert state.status == "paused" and state.paused_reason == "needs_input"
    assert len(_notices(server, "k3.goal.blocked")) == 1
    await server.close()


async def test_interrupted_turn_pauses_goal_with_a_reason_and_notifies(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    sid = await new_session(server, repo)
    _goal(server, sid).set("get interrupted")

    async def cancelled(self, *args, **kwargs):
        raise TurnCancelled("cancelled while parked")
        yield  # pragma: no cover

    monkeypatch.setattr(AgentLoop, "run", cancelled)
    live = server.live_for(server.store.get(sid))
    status, _ = await server._run_turn(live, "go")
    assert status == "interrupted"
    state = _goal(server, sid).state
    assert state.status == "paused" and state.paused_reason == "interrupted"
    assert len(_notices(server, "k3.goal.blocked")) == 1
    await server.close()


async def test_exhausted_providers_pause_the_goal_with_a_reason_and_one_notification(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    sid = await new_session(server, repo)
    _goal(server, sid).set("needs a provider")

    async def down(self, *args, **kwargs):
        raise ChainExhausted("all provider entries failed (last reason: server)", last_reason="server")
        yield  # pragma: no cover

    monkeypatch.setattr(AgentLoop, "run", down)
    result = await ServerRunner(server).run_prompt("go", session_id=sid)
    assert result.status == "failed"  # the run failed only after the retries the reliability layer allowed
    state = _goal(server, sid).state
    assert state.status == "paused" and state.paused_reason == "provider unavailable"
    assert len(_notices(server, "k3.goal.blocked")) == 1
    await server.close()


def _mem_manager(**kw) -> GoalManager:
    box: dict[str, dict | None] = {"goal": None}
    return GoalManager(lambda: box["goal"], lambda s: box.__setitem__("goal", s), **kw)


async def test_goal_without_turn_budget_never_pauses_and_reads_without_a_limit():
    mgr = _mem_manager()  # the default: no turn budget (it was 300)
    st = mgr.set("keep going")
    assert st.max_turns == 0 and mgr.snapshot()["max_turns"] == 0
    judge = scripted_judge(["continue"])
    for _ in range(400):
        decision = await mgr.evaluate_after_turn("still working", judge)
        assert decision.should_continue and decision.prompt
    assert mgr.state.status == "active" and mgr.state.turns_used == 400
    assert decision.message == "↻ Continuing toward goal (turn 400): verdict continue"
    assert mgr.status_line().startswith("Goal (active, 400 turns): keep going")


async def test_a_finite_turn_budget_still_pauses_and_an_explicit_zero_overrides_a_configured_one():
    mgr = _mem_manager(default_max_turns=2)
    mgr.set("bounded")
    judge = scripted_judge(["continue"])
    first = await mgr.evaluate_after_turn("x", judge)
    assert first.message == "↻ Continuing toward goal (1/2): verdict continue"
    second = await mgr.evaluate_after_turn("x", judge)
    assert not second.should_continue and mgr.state.paused_reason == "turn budget exhausted (2/2)"
    assert mgr.set("unbounded", max_turns=0).max_turns == 0  # /goal --turns 0: no limit despite the config


async def test_failing_check_retries_without_limit_by_default_and_a_finite_limit_still_pauses(monkeypatch):
    import k3code.goals as goals

    async def failing_gate(gate, *, cwd=None):
        return False, 1, "nope"

    monkeypatch.setattr(goals, "run_gate", failing_gate)
    mgr = _mem_manager()
    mgr.set("ship", check="exit 1")
    judge = scripted_judge(["done"])
    for n in range(1, 11):
        decision = await mgr.evaluate_after_turn("done!", judge)
        assert decision.should_continue and decision.verdict == "gate_failed"
        assert f"(attempt {n}):" in decision.prompt
    assert decision.message == "✗ Check failed (10 turns, attempt 10): $ exit 1"

    capped = _mem_manager()
    st = capped.set("ship", check="exit 1")
    st.gates[0].max_retries = 2
    capped._save(st)
    assert (await capped.evaluate_after_turn("done!", judge)).message.endswith("attempt 1/2): $ exit 1")
    await capped.evaluate_after_turn("done!", judge)
    paused = await capped.evaluate_after_turn("done!", judge)
    assert not paused.should_continue and capped.state.paused_reason == "check exhausted 2 retries: $ exit 1"
