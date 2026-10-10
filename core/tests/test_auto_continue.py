"""``autonomy.auto_continue``: an ordinary prompt runs as an implicit goal that ends cleared, never paused."""

from __future__ import annotations

from k3code.agent.loop import AgentLoop
from k3code.automation.server_runner import ServerRunner
from k3code.goals import GoalState
from k3code.reliability import BudgetExceeded
from k3code.reliability.persistent_retry import TurnCancelled
from m1cmd_helpers import git_repo, make_server, new_session

AUTO = {"advisor_on_goal": False, "auto_continue": True}


def scripted_judge(verdicts):
    calls: list[tuple[str, str]] = []

    async def judge(goal: str, response: str):
        calls.append((goal, response))
        v = verdicts[min(len(calls) - 1, len(verdicts) - 1)]
        return v, f"verdict {v}", False, False

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def _goal_notices(server) -> list[dict]:
    shown = [e["payload"] for e in server.event_log if e["type"] == "notification.show"]
    return [p for p in shown if p.get("kind") == "goal" or str(p.get("key", "")).startswith(("goal", "k3.goal"))]


def _implicit_goal_shown(server) -> bool:
    """The goal bar got an implicit goal at some point (so a test that ends with no goal saw one cleared)."""
    updates = [e["payload"] for e in server.event_log if e["type"] == "session.control.update"]
    return any(isinstance(g := u["control"].get("goal"), dict) and g.get("implicit") for u in updates)


async def _setup(tmp_path, monkeypatch, replies=None, autonomy=AUTO):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=replies, autonomy=autonomy)
    sid = await new_session(server, repo)
    live = server.live[sid]
    return server, provider, live, server.goal_manager(live)


async def test_an_ordinary_prompt_continues_until_the_judge_says_done_then_the_goal_is_cleared(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["step 1", "step 2", "all built"])
    judge = server.goal_judge = scripted_judge(["continue", "continue", "done"])
    status, text = await server._run_turn(live, "build the parser")
    assert status == "done" and text == "all built"
    assert provider.n == 3 and len(judge.calls) == 3
    assert {goal for goal, _ in judge.calls} == {"build the parser"}  # the prompt is the goal
    assert "build the parser" in str(provider.seen[1][-1].content)  # the continuation drives turn 2
    assert mgr.state is None  # cleared, not left "done" in the goal bar
    await server.close()


async def test_a_chat_reply_judged_done_is_one_turn_and_sends_no_goal_notice(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["4"])
    judge = server.goal_judge = scripted_judge(["done"])
    await server._run_turn(live, "what is 2+2?")
    assert provider.n == 1 and len(judge.calls) == 1
    assert mgr.state is None
    assert _goal_notices(server) == []
    await server.close()


async def test_a_blocked_verdict_clears_the_implicit_goal_without_a_resume_notice(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["I need the API key"])
    judge = server.goal_judge = scripted_judge(["blocked"])
    await server._run_turn(live, "deploy it")
    assert provider.n == 1 and len(judge.calls) == 1 and mgr.state is None
    assert _goal_notices(server) == []
    await server.close()


async def test_an_interrupted_implicit_goal_is_cleared_not_paused(tmp_path, monkeypatch):
    server, _, live, mgr = await _setup(tmp_path, monkeypatch)
    server.goal_judge = scripted_judge(["continue"])

    async def cancelled(self, *args, **kwargs):
        raise TurnCancelled("cancelled while parked")
        yield  # pragma: no cover

    monkeypatch.setattr(AgentLoop, "run", cancelled)
    status, _ = await server._run_turn(live, "refactor everything")
    assert status == "interrupted"
    assert mgr.state is None and _implicit_goal_shown(server)
    assert _goal_notices(server) == []
    await server.close()


async def test_needs_input_clears_the_implicit_goal(tmp_path, monkeypatch):
    server, _, live, mgr = await _setup(tmp_path, monkeypatch)
    server.goal_judge = scripted_judge(["continue"])

    async def over_budget(self, *args, **kwargs):
        raise BudgetExceeded("session token budget exceeded")
        yield  # pragma: no cover

    monkeypatch.setattr(AgentLoop, "run", over_budget)
    status, _ = await server._run_turn(live, "spend it all")
    assert status == "needs_input"
    assert mgr.state is None and _implicit_goal_shown(server)
    assert _goal_notices(server) == []
    await server.close()


async def test_a_real_goal_still_pauses_and_a_prompt_meanwhile_does_not_replace_it(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch)
    judge = server.goal_judge = scripted_judge(["continue"])
    mgr.set("ship the release")

    async def cancelled(self, *args, **kwargs):
        raise TurnCancelled("cancelled")
        yield  # pragma: no cover

    with monkeypatch.context() as m:
        m.setattr(AgentLoop, "run", cancelled)
        await server._run_turn(live, "go")
    state = mgr.state
    assert state.status == "paused" and state.paused_reason == "interrupted" and not state.implicit
    assert len(_goal_notices(server)) == 1

    await server._run_turn(live, "yes, use the staging key")  # the answer the paused goal waits for
    state = mgr.state
    assert state.goal == "ship the release" and state.status == "paused" and not state.implicit
    assert judge.calls == []  # a plain turn: nothing judged it
    await server.close()


async def test_auto_continue_off_makes_no_judge_call(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, autonomy={"advisor_on_goal": False})
    judge = server.goal_judge = scripted_judge(["done"])
    await server._run_turn(live, "next I would refactor")
    assert provider.n == 1 and judge.calls == []
    assert mgr.state is None and not _implicit_goal_shown(server)
    await server.close()


async def test_background_and_automation_turns_never_auto_continue(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch)
    judge = server.goal_judge = scripted_judge(["done"])
    result = await ServerRunner(server).run_prompt("tick", session_id=live.session_id)  # a loop/cron/automation run
    assert result.status == "completed"
    live.background = True  # /bg, Ctrl+B
    await server._run_turn(live, "background work")
    assert provider.n == 2 and judge.calls == []
    assert mgr.state is None and not _implicit_goal_shown(server)
    await server.close()


async def test_a_queued_prompt_replaces_the_implicit_goal(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["first", "second"])
    judge = server.goal_judge = scripted_judge(["done"])
    live.pending_prompts.append("actually, do this instead")  # typed while the first turn ran
    await server._run_turn(live, "first task")
    assert provider.n == 2
    assert [goal for goal, _ in judge.calls] == ["actually, do this instead"]  # the first one ended unjudged
    assert mgr.state is None
    await server.close()


async def test_a_queued_prompt_after_a_continued_turn_is_a_goal_of_its_own(tmp_path, monkeypatch):
    """A turn that ended right after a continuation left ``goal_continuation`` set; the queued prompt run next then
    counted as a continuation itself and got no implicit goal."""
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["step 1", "step 2", "third"])
    inner = scripted_judge(["continue", "done"])

    async def judge(goal, response):
        if not inner.calls:
            live.pending_prompts.append("then do this")  # typed while the first turn ran
        return await inner(goal, response)

    server.goal_judge = judge
    await server._run_turn(live, "first task")
    assert provider.n == 3
    # turn 2 ended with a prompt waiting, so it went unjudged; the waiting prompt is judged as the goal it now is
    assert inner.calls == [("first task", "step 1"), ("then do this", "third")]
    assert mgr.state is None
    await server.close()


async def test_kicks_never_resurrect_an_implicit_goal(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch)
    mgr.set("a prompt from before the daemon died", implicit=True)
    assert await server.watchdog_tick(source="boot") == []
    assert provider.n == 0 and mgr.state is None
    await server.close()


def test_implicit_is_persisted_in_the_snapshot_and_old_states_load_without_it():
    state = GoalState(goal="g", implicit=True)
    assert GoalState.from_dict(state.to_dict()).implicit is True
    assert state.snapshot()["implicit"] is True
    old = state.to_dict()
    del old["implicit"]
    assert GoalState.from_dict(old).implicit is False
