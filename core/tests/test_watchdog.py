"""P1-4: the goal watchdog re-kicks active goals that have no live turn; a fourth kick within an hour parks the goal."""

from __future__ import annotations

import asyncio
import time

from k3code.goals import KICK_WINDOW_S
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


def _active_goal(server, sid, objective="keep the build green"):
    live = server.live_for(server.store.get(sid))
    mgr = server.goal_manager(live)
    mgr.set(objective)
    return live, mgr


async def test_watchdog_gives_a_persisted_active_goal_one_kick(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=["green"], autonomy=NO_ADVISOR)
    sid = await new_session(server, repo)
    live, mgr = _active_goal(server, sid)
    server.goal_judge = scripted_judge(["done"])
    assert await server.watchdog_tick() == [sid]
    await asyncio.wait_for(live.turn_task, 20)
    assert provider.n == 1 and len(server.goal_judge.calls) == 1
    assert mgr.state.status == "done"
    assert await server.watchdog_tick() == []  # done goals are not kicked
    await server.close()


async def test_halted_or_storm_paused_means_zero_kicks(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    sid = await new_session(server, repo)
    live, mgr = _active_goal(server, sid)
    server.goal_judge = scripted_judge(["done"])

    await server.halt_daemon("held")
    assert await server.watchdog_tick() == []
    server.resume_daemon()
    mgr.resume()

    server.background_paused = True  # restart-storm safe mode
    assert await server.watchdog_tick() == []
    server.background_paused = False
    assert provider.n == 0 and mgr.state.status == "active"
    assert await server.watchdog_tick() == [sid]  # the same goal is kicked once the guards allow it
    await asyncio.wait_for(live.turn_task, 20)
    await server.close()


async def test_a_fourth_kick_within_an_hour_parks_the_goal_and_notifies(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    sid = await new_session(server, repo)
    live, mgr = _active_goal(server, sid)
    now = time.time()
    for ago in (100, 50, 10):  # three kicks in the last hour already
        mgr.record_kick(now - ago)

    assert await server.watchdog_tick() == []
    assert provider.n == 0
    assert mgr.state.status == "paused" and mgr.state.paused_reason.startswith("parked:")
    parked = [e["payload"] for e in server.event_log if e["type"] == "notification.show"
              and "parked" in str(e["payload"].get("key"))]
    assert len(parked) == 1 and parked[0]["level"] == "warning"
    await server.close()


async def test_kicks_older_than_an_hour_do_not_count(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=["green"], autonomy=NO_ADVISOR)
    sid = await new_session(server, repo)
    live, mgr = _active_goal(server, sid)
    now = time.time()
    for ago in (KICK_WINDOW_S + 300, KICK_WINDOW_S + 200, KICK_WINDOW_S + 100):
        mgr.record_kick(now - ago)
    server.goal_judge = scripted_judge(["done"])
    assert await server.watchdog_tick() == [sid]
    await asyncio.wait_for(live.turn_task, 20)
    assert provider.n == 1
    await server.close()


async def test_finished_or_paused_goals_never_become_live_sessions(tmp_path, monkeypatch):
    from k3code.goals import GoalState

    server, provider = make_server(tmp_path, monkeypatch, replies=["x"], autonomy=NO_ADVISOR)
    done = server.store.create(cwd=str(tmp_path))
    done.meta["goal"] = GoalState(goal="finished", status="done").to_dict()
    server.store.save(done)
    paused = server.store.create(cwd=str(tmp_path))
    paused.meta["goal"] = GoalState(goal="held", status="paused", paused_reason="needs_input").to_dict()
    server.store.save(paused)
    assert await server.watchdog_tick() == []
    assert done.session_id not in server.live and paused.session_id not in server.live  # no strip clutter
    assert provider.n == 0
    await server.close()
