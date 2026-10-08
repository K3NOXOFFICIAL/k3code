"""Goal lifecycle across the daemon: boot resume (P1-1), graceful stop, kicks and the halt interplay."""

from __future__ import annotations

import asyncio

from k3code.gateway.sessions import SessionStore
from k3code.goals import GoalManager, GoalState
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
