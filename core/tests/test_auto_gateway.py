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


async def test_automations_command_and_session_event_trigger(tmp_path, monkeypatch):
    from m1cmd_helpers import submit_and_wait

    server, provider = make_server(tmp_path, monkeypatch, ["done"])
    clock = FakeClock()
    eng = await engine_for(server, clock)
    sid = await new_session(server, tmp_path)
    out = await cmd(
        server,
        "/automations add {name: ping, trigger: {type: session_event, event: completed}, "
        "action: {type: notify, text: 'finished {{session}}'}}",
        sid,
    )
    assert "Added automation" in out["output"]
    assert "ping" in (await cmd(server, "/automations list", sid))["output"]
    await submit_and_wait(server, "hello")
    await until(lambda: eng.db.rows("automations")[0]["fire_count"] == 1)
    assert any(f"finished {sid}" in (n.get("text") or "") for n in notices(server))
    bad = await cmd(
        server, "/automations add {name: x, trigger: {type: webhook}, action: {type: notify, text: a}}", sid
    )
    assert "webhook triggers are disabled" in bad["output"]
    assert "Paused" in (await cmd(server, "/automations pause ping", sid))["output"]
    assert "Removed" in (await cmd(server, "/automations rm ping", sid))["output"]
    counts = server.automation.counts()
    assert counts["automations"] == 0
    await eng.stop()


def notices(server):
    import json

    out = []
    for f in server._frames:
        m = json.loads(f)
        if m.get("params", {}).get("type") == "notification.show":
            out.append(m["params"]["payload"])
    return out


async def test_suggest_accept_dismiss_via_command(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    eng = await engine_for(server, FakeClock())
    sid = await new_session(server, tmp_path)
    listing = (await cmd(server, "/automations suggest", sid))["output"]
    assert "Nightly test run" in listing and "needs input" in listing
    assert "Dismissed" in (await cmd(server, "/automations suggest dismiss 1", sid))["output"]
    assert "Nightly test run" not in (await cmd(server, "/automations suggest", sid))["output"]
    out = (await cmd(server, "/automations suggest accept 1", sid))["output"]
    assert "Created automation" in out and eng.automations.active_count() == 1
    await eng.stop()


async def test_active_list_reports_automation_counts(tmp_path, monkeypatch):
    from m1cmd_helpers import rpc

    server, _ = make_server(tmp_path, monkeypatch)
    eng = await engine_for(server, FakeClock())
    sid = await new_session(server, tmp_path)
    await cmd(server, "/loop 5m x", sid)
    await cmd(server, '/schedule add "* * * * *" y', sid)
    res = (await rpc(server, "session.active_list"))["result"]
    assert res["automation"]["loops"] == 1 and res["automation"]["jobs"] == 1 and res["automation"]["active"] == 2
    await eng.stop()


async def test_finished_cron_sessions_release_resources(tmp_path, monkeypatch):
    from k3code.automation import server_runner

    monkeypatch.setattr(server_runner, "KEEP_FINISHED_RUNS", 2)
    server, provider = make_server(tmp_path, monkeypatch, ["ok"])
    eng = await engine_for(server, FakeClock())
    for i in range(4):
        res = await eng.runner.run_prompt(f"job {i}", name="cron: t")
        assert res.status == "completed"
    runs = [s for s in server.live.values() if s.stored.meta.get("origin") == "automation"]
    assert len(runs) == 2  # older ones evicted from memory...
    assert len(server.store.list(limit=50)) >= 4  # ...but still stored
    assert all(s.reliability is None or not s.reliability._started for s in runs)
    await eng.stop()


async def test_unattended_kinds_use_cheap_tier_and_skip_gate(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, ["ok"])
    server.config.autonomy = {"plan_first": True}
    clock = FakeClock()
    eng = await engine_for(server, clock)
    asked: list[str] = []
    tr = server.tier_routers()
    orig_get = tr.get
    tr.get = lambda tier: (asked.append(str(tier)), orig_get(tier))[1]  # type: ignore[method-assign]
    sid = await new_session(server, tmp_path)
    await cmd(server, '/schedule add "* * * * *" "say hi" --name hi', sid)
    await clock.advance(61)
    await until(lambda: provider.n >= 1)
    await asyncio.sleep(0.1)
    row = server.usage.aggregate(by="day")[0]
    assert row["by_kind"].get("cron_job", 0) >= 1, row
    assert "classification" not in row["by_kind"]  # unattended: pre-approved, no scope gate
    assert asked == ["cheap"], asked  # the policy routes cron_job to the cheap tier
    await eng.stop()


def test_gate_unattended_option(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from k3code.autonomy.plan_first import PlanFirst

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    pf = PlanFirst(server)
    s = SimpleNamespace(background=True, scope_override=None, perms=SimpleNamespace(mode=SimpleNamespace(value="auto")))
    assert pf.gate_applies(s) is False
    server.config.autonomy = {"gate_unattended": True}
    assert pf.gate_applies(s) is True


async def test_stats_command_with_days_filter(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, ["ok"])
    sid = await new_session(server, tmp_path)
    server.usage.record("call", session=sid, provider="p", model="m", tier="cheap", task_kind="cron_job")
    out = await cmd(server, "/stats day 7", sid)
    assert "cron_job" in out["output"] and "cheap" in out["output"]
    assert server.usage.aggregate("session", days=1)


async def test_goal_automations_do_not_leak_sessions_or_netwatch_tasks(tmp_path, monkeypatch):
    """start_goal created a session then ran it as an "existing" one, so run_prompt never released it: every fire of a
    goal automation left a LiveSession and a started NetWatch (two forever-tasks) behind."""
    from k3code.automation.server_runner import KEEP_FINISHED_RUNS, ServerRunner

    server, provider = make_server(tmp_path, monkeypatch, ["goal reply"])
    runner = ServerRunner(server)
    async with asyncio.timeout(60):  # a leak used to hang the shutdown below
        for _ in range(KEEP_FINISHED_RUNS + 6):
            await runner.start_goal("tidy the repo", None, str(tmp_path))
        unattended = [s for s in server.live.values() if s.stored.meta.get("origin") == runner.origin]
        assert len(unattended) <= KEEP_FINISHED_RUNS
        # none of them keeps polling the network while idle
        assert all(s.reliability is None or not s.reliability._started for s in unattended)
        await server.close()


async def test_two_unattended_runs_on_one_session_do_not_overlap(tmp_path, monkeypatch):
    """run_prompt's busy check and the turn's own `streaming` flag were separated by awaits: two callers both passed
    it, two AgentLoops interleaved on one conversation and `background` was left stuck True."""
    from k3code.automation.server_runner import ServerRunner

    server, provider = make_server(tmp_path, monkeypatch, ["tick reply"])
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    assert live.background is False
    active = peak = 0
    real = server._run_turn_locked

    async def slow(session, text):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.05)
            return await real(session, text)
        finally:
            active -= 1

    server._run_turn_locked = slow  # type: ignore[method-assign]
    runner = ServerRunner(server)
    async with asyncio.timeout(30):
        results = await asyncio.gather(
            runner.run_prompt("task A", session_id=sid), runner.run_prompt("task B", session_id=sid)
        )
    assert peak == 1, "two turns ran at once on one session"
    assert [r.status for r in results] == ["completed", "completed"]
    assert live.background is False  # restored, not left True by the second caller
    assert sum(1 for m in live.stored.messages if m["role"] == "user") == 2  # neither tick's history was lost


async def test_automation_shell_probes_bwrap_off_the_event_loop(tmp_path, monkeypatch):
    """ServerRunner.run_shell called sandbox.usable() on the event loop; that probe spawns bwrap (up to 10 s)."""
    import threading

    from k3code.automation.server_runner import ServerRunner
    from k3code.reliability import sandbox

    probed_on: list[threading.Thread] = []

    def fake_usable() -> bool:
        probed_on.append(threading.current_thread())
        return False  # bwrap unusable: the command runs unsandboxed and says so

    monkeypatch.setattr(sandbox, "usable", fake_usable)
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    code, out = await ServerRunner(server).run_shell("echo shell-ok", str(tmp_path))
    assert code == 0 and "shell-ok" in out and "bwrap unavailable" in out
    assert probed_on and probed_on[0] is not threading.main_thread()
    await server.close()


async def test_unattended_runs_refuse_to_default_to_the_daemons_home_cwd(tmp_path, monkeypatch):
    """A daemon started in $HOME gave every job without a cwd the whole home directory as its project."""
    from k3code.automation.server_runner import ServerRunner

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    runner = ServerRunner(server)
    code, out = await runner.run_shell("echo hi", "")
    assert code != 0 and "explicit cwd" in out
    for res in (await runner.run_prompt("hi"), await runner.start_goal("goal", None, "")):
        assert res.status == "failed" and "explicit cwd" in res.error
    eng = await engine_for(server, FakeClock())
    out = await cmd(server, '/schedule add "* * * * *" "say hi"', None)  # no session, no --cwd
    assert "--cwd" in out["output"] and eng.jobs.active_count() == 0
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    assert (await runner.run_prompt("hi")).status == "completed"
    await eng.stop()
    await server.close()
