"""Loops and cron jobs end-to-end through the gateway (real AgentLoop, scripted provider, fake clock)."""

import asyncio

from k3code.automation.clock import FakeClock
from k3code.automation.engine import AutomationEngine
from m1cmd_helpers import cmd, make_server, new_session


async def noop():
    return None


async def until(cond, tries=400):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def engine_for(server, clock):
    eng = AutomationEngine(server, clock=clock, wait_online=noop)
    server.automation = eng
    await eng.start()
    return eng


async def test_loop_command_runs_ticks_in_same_session(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, ["tick reply"])
    clock = FakeClock()
    eng = await engine_for(server, clock)
    sid = await new_session(server, tmp_path)
    out = await cmd(server, "/loop 5m watch the build --times 2", sid)
    assert "started" in out["output"]
    await until(lambda: provider.n >= 1)
    live = server.live[sid]
    await until(lambda: not live.streaming and live.state == "completed")
    assert "watch the build" in str(provider.seen[0][-1].content)
    await clock.advance(301)
    await until(lambda: provider.n >= 2 and not live.streaming)
    await asyncio.sleep(0.05)
    assert eng.loops.list()[0]["state"] == "done"
    # both ticks are in the one session's history
    assert sum(1 for m in live.stored.messages if m["role"] == "user") == 2
    assert not live.background  # restored after the tick
    status = await cmd(server, "/loop list", sid)
    assert "done" in status["output"]
    await eng.stop()


async def test_schedule_add_list_run(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, ["cron reply"])
    clock = FakeClock()
    eng = await engine_for(server, clock)
    sid = await new_session(server, tmp_path)
    out = await cmd(server, '/schedule add "* * * * *" "say hi" --name hi', sid)
    assert "Scheduled" in out["output"]
    await clock.advance(61)
    await until(lambda: provider.n >= 1)
    await until(lambda: eng.db.runs(eng.db.rows("jobs")[0]["id"])[0]["status"] == "completed")
    listing = (await cmd(server, "/schedule list", sid))["output"]
    assert "hi" in listing and "completed" in listing
    # the run was a separate background session in auto mode
    run_sessions = [s for s in server.live.values() if s.stored.meta.get("origin") == "automation"]
    assert run_sessions and run_sessions[0].stored.meta["mode"] == "auto" and run_sessions[0].background
    assert run_sessions[0].state == "completed"
    assert "Paused" in (await cmd(server, "/schedule pause hi", sid))["output"]
    await eng.stop()


async def test_schedule_natural_language_confirmed(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, ["0 9 * * 1-5"])
    server.config.goal.judge_model = "default"
    clock = FakeClock()
    eng = await engine_for(server, clock)
    sid = await new_session(server, tmp_path)

    async def clarify(q, choices, session_id):
        assert "0 9 * * 1-5" in q
        return {"answer": "Yes"}

    server.clarify = clarify  # type: ignore[method-assign]
    out = await cmd(server, '/schedule add "every weekday at 9" "morning report"', sid)
    assert "0 9 * * 1-5" in out["output"]
    assert eng.db.rows("jobs")[0]["schedule"]["expr"] == "0 9 * * 1-5"

    async def deny(q, choices, session_id):
        return {"answer": "No"}

    server.clarify = deny  # type: ignore[method-assign]
    out = await cmd(server, '/schedule add "every weekday at 9" "again"', sid)
    assert "Cancelled" in out["output"] and len(eng.db.rows("jobs")) == 1
    await eng.stop()
