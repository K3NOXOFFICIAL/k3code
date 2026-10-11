"""Audit findings in the automation package: one engine per home, no self-retriggering, gates, cron parsing."""

import asyncio
import socket
import subprocess
import time
import types
from datetime import UTC, datetime

import pytest

from auto_helpers import FakeRunner, clock, make_db
from k3code.automation import clock as clock_mod
from k3code.automation.automations import DEFAULT_POLICY, AutomationError, AutomationManager
from k3code.automation.clock import FakeClock, SystemClock
from k3code.automation.cronexpr import (
    MIN_INTERVAL_S,
    ScheduleError,
    cron_next,
    parse_cron,
    parse_interval,
    parse_schedule,
)
from k3code.automation.engine import AutomationEngine
from k3code.automation.loops import MAX_CONSECUTIVE_FAILURES, LoopManager
from k3code.automation.runner import PACING_MIN_S, RunResult
from k3code.automation.triggers import FileChangeTrigger, GitTrigger
from k3code.halt import Halt
from m1cmd_helpers import cmd, git_repo, make_server, new_session


async def noop():
    return None


async def until(cond, tries=300, step=0.02):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition never became true")


def ts(*a):
    return datetime(*a, tzinfo=UTC).timestamp()


# ── 1: one automation engine per home ─────────────────────────────────────────


async def two_engines(tmp_path, monkeypatch):
    s1, p1 = make_server(tmp_path, monkeypatch, ["ok"])
    s2, p2 = make_server(tmp_path, monkeypatch, ["ok"])  # a second gateway process on the same K3CODE_HOME
    c1, c2 = FakeClock(), FakeClock()
    e1 = AutomationEngine(s1, clock=c1, wait_online=noop, use_netwatch=False)
    e2 = AutomationEngine(s2, clock=c2, wait_online=noop, use_netwatch=False)
    s1.automation, s2.automation = e1, e2
    return (s1, p1, c1, e1), (s2, p2, c2, e2)


async def test_second_engine_on_one_home_neither_fires_nor_interrupts_live_runs(tmp_path, monkeypatch):
    (s1, p1, c1, e1), (s2, p2, c2, e2) = await two_engines(tmp_path, monkeypatch)
    await e1.start()
    yearly = e1.jobs.add(prompt="yearly", schedule="0 0 1 1 *", name="yearly", cwd=str(tmp_path))
    live_run = e1.db.insert("job_runs", owner=yearly["id"], owner_kind="job", started_at=c1.now(), status="running")
    await e2.start()  # used to mark the daemon's live run interrupted and start its own scheduler
    assert e1.db.get("job_runs", live_run)["status"] == "running"
    # a job added through the passive engine is still run, once, by the holder
    job = e2.jobs.add(prompt="say hi", schedule="* * * * *", name="hi", cwd=str(tmp_path))
    for _ in range(3):  # both processes wake for the same occurrence: it used to run twice
        await asyncio.gather(c1.advance(60), c2.advance(60))
        await asyncio.sleep(0.3)
    await until(lambda: len(e1.db.runs(job["id"], 50)) >= 3 and p1.n + p2.n >= 3)
    await asyncio.sleep(0.2)
    assert len(e1.db.runs(job["id"], 50)) == 3 and p1.n + p2.n == 3
    assert e1.active and not e2.active
    await e2.stop()
    await e1.stop()


async def test_passive_engine_manages_definitions_but_arms_nothing(tmp_path, monkeypatch):
    (s1, _, c1, e1), (s2, _, c2, e2) = await two_engines(tmp_path, monkeypatch)
    await e1.start()
    await e2.start()
    sid = await new_session(s2, tmp_path)
    out = await cmd(s2, "/loop 5m watch the build", sid)
    assert out.get("error") == "command_failed" and "another k3code process" in out["output"]
    assert "k3code attach" in out["output"]  # a throwaway `k3code slash` session would not do for a session-bound loop
    assert e2.db.rows("loops") == []  # nothing stored that would sit 'active' and never tick
    out = await cmd(s2, "/automations add {name: x, trigger: {type: net_state}, action: {type: notify, text: a}}", sid)
    assert "another k3code process" in out["output"] and e2.db.rows("automations") == []
    assert "Scheduled" in (await cmd(s2, '/schedule add "0 9 * * *" "daily" --name d', sid))["output"]
    assert (await cmd(s2, "/schedule pause d", sid))["output"].startswith("Paused")
    # an automation armed by the holder is gone as soon as the passive side removes the row: fire() re-reads it
    a = e1.automations.add(name="n", trigger={"type": "net_state"}, action={"type": "notify", "text": "hi"})
    assert await e2.automations.remove(a["id"])
    assert await e1.automations.fire(a["id"], {"event": "x"}) is None
    # a resume the passive side cannot arm is refused, not reported as done: the holder disarmed it when it paused
    b = e1.automations.add(name="b", trigger={"type": "net_state"}, action={"type": "notify", "text": "hi"})
    assert await e1.automations.pause(b["id"])
    with pytest.raises(AutomationError, match="another k3code process"):
        await e2.automations.resume(b["id"])
    assert e2.db.get("automations", b["id"])["state"] == "paused"
    assert await e1.automations.resume(b["id"])
    await e2.stop()
    await e1.stop()


async def test_passive_engine_takes_over_when_the_holder_exits(tmp_path, monkeypatch):
    (s1, _, c1, e1), (s2, p2, c2, e2) = await two_engines(tmp_path, monkeypatch)
    await e1.start()
    await e2.start()
    assert not e2.active
    await c2.advance(31)
    assert not e2.active  # the holder still lives
    await e1.stop()
    await c2.advance(31)
    await until(lambda: e2.active)
    assert e2.loops.active and e2.automations.active
    await e2.stop()


async def test_forced_test_run_still_works_on_a_passive_manager(tmp_path):
    c, db, r = clock(), make_db(tmp_path), FakeRunner()
    m = AutomationManager(db, r, c)
    a = m.add(name="n", trigger={"type": "net_state"}, action={"type": "notify", "text": "hi"})
    m.active = False
    assert await m.fire(a["id"], {"event": "net_online"}) is None and r.notes == []
    assert (await m.test(a["id"])).startswith("Test run completed") and r.notes[0][0] == "hi"
    with pytest.raises(AutomationError, match="another k3code process"):
        m.add(name="m", trigger={"type": "net_state"}, action={"type": "notify", "text": "hi"})
    await m.stop()


# ── 2: event triggers do not retrigger themselves ────────────────────────────


async def test_file_change_ignores_what_its_own_action_wrote(tmp_path):
    fired: list[dict] = []

    async def fire(info):
        fired.append(info)
        (tmp_path / "out.py").write_text(str(len(fired)))  # the action writes inside the watched tree
        await asyncio.sleep(0.2)

    t = FileChangeTrigger(
        {"type": "file_change", "glob": "**/*.py", "debounce": 0.05}, fire, clock(), cwd=str(tmp_path)
    )
    t.start()
    await asyncio.wait_for(t.ready.wait(), 5)
    await asyncio.sleep(0.3)  # let inotify arm
    (tmp_path / "a.py").write_text("x = 1")
    await until(lambda: fired, tries=200)
    await asyncio.sleep(1.5)  # it used to queue its own writes and fire again as soon as the action returned
    assert len(fired) == 1 and fired[0]["path"] == "a.py"
    (tmp_path / "b.py").write_text("y = 2")  # a real edit after the run still fires
    await until(lambda: len(fired) == 2, tries=200)
    assert fired[1]["path"] == "b.py"
    await t.stop()


async def test_git_trigger_does_not_refire_for_the_commit_its_action_made(tmp_path):
    repo = git_repo(tmp_path / "r")
    c = clock()
    fired: list[dict] = []

    async def fire(info):
        fired.append(info)
        (repo / "auto.txt").write_text(str(len(fired)))
        for argv in (["add", "-A"], ["commit", "-qm", "by the action"]):
            subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)

    t = GitTrigger({"type": "git", "event": "commit", "branch": "main"}, fire, c, cwd=str(repo), poll_s=10)
    t.start()
    await until(lambda: t.last)
    (repo / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "user commit"], cwd=repo, check=True, capture_output=True)
    await c.advance(10)
    await until(lambda: fired)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    await until(lambda: t.last == head)  # the action's own commit is absorbed
    for _ in range(3):
        await c.advance(10)
        await asyncio.sleep(0.2)
    assert len(fired) == 1
    await t.stop()


async def test_event_triggers_get_a_default_cooldown_and_rate_limit_that_explicit_policy_overrides(tmp_path):
    c, db, r = clock(), make_db(tmp_path), FakeRunner()
    m = AutomationManager(db, r, c, webhook_port=0)
    await m.start()
    note = {"type": "notify", "text": "x"}
    fc = m.add(name="fc", trigger={"type": "file_change", "glob": "*.zz"}, action=note, cwd=str(tmp_path))
    hook = m.add(name="hook", trigger={"type": "webhook"}, action=note)
    free = m.add(
        name="free",
        trigger={"type": "git"},
        action=note,
        policy={"cooldown_s": 0, "max_per_hour": 0},
        cwd=str(tmp_path),
    )
    assert set(DEFAULT_POLICY) == {"file_change", "git", "webhook"}
    for row in (fc, hook):
        await m.fire(row["id"], {"event": "e"})
        await m.fire(row["id"], {"event": "e"})  # same instant: inside the cooldown
        assert db.get("automations", row["id"])["fire_count"] == 1
    for _ in range(3):
        await m.fire(free["id"], {"event": "e"})
    assert db.get("automations", free["id"])["fire_count"] == 3
    # the hourly cap: a cooldown-free file_change automation stops at the default cap
    cap = int(DEFAULT_POLICY["file_change"]["max_per_hour"])
    db.update("automations", fc["id"], policy={"cooldown_s": 0})
    for _ in range(cap + 5):
        await m.fire(fc["id"], {"event": "e"})
    assert db.get("automations", fc["id"])["fire_count"] == cap
    await m.stop()


# ── 3: a busy webhook port does not take the daemon down ──────────────────────


async def test_busy_webhook_port_is_logged_and_the_manager_starts(tmp_path, caplog):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    try:
        m = AutomationManager(make_db(tmp_path), FakeRunner(), clock(), webhook_port=port)
        caplog.set_level("ERROR", logger="k3code.automation.automations")
        await m.start()  # OSError (Errno 98) used to propagate to run_daemon: no READY, restart loop, safe mode
        assert m.webhook is None
        assert any(str(port) in rec.getMessage() for rec in caplog.records)
        await m.stop()
    finally:
        blocker.close()


async def test_engine_starts_when_the_configured_webhook_port_is_busy(tmp_path, monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        server, _ = make_server(tmp_path, monkeypatch, ["ok"], automation={"webhook_port": blocker.getsockname()[1]})
        eng = AutomationEngine(server, clock=FakeClock(), use_netwatch=False)
        server.automation = eng
        await eng.start()  # run_daemon awaits exactly this before READY=1
        assert eng.active and eng.automations.webhook is None
        await eng.stop()
        await server.close()
    finally:
        blocker.close()


# ── 4: failed loop ticks ──────────────────────────────────────────────────────


async def test_failed_ticks_do_not_burn_the_budget_and_five_in_a_row_stop_the_loop_with_a_notice(tmp_path):
    c, db = clock(), make_db(tmp_path)
    r = FakeRunner([RunResult(status="failed", error="provider down")])
    m = LoopManager(db, r, c)
    row = m.create(session_id="s", prompt="p", interval="1m", max_ticks=3)
    await c.settle()
    await c.advance(60 * 10)
    loop = db.get("loops", row["id"])
    assert len(r.prompts) == MAX_CONSECUTIVE_FAILURES  # stopped at the streak, not at max_ticks / "done"
    assert loop["state"] == "failed" and loop["ticks"] == 0
    assert "in a row failed" in loop["stop_reason"] and "provider down" in loop["stop_reason"]
    assert [n for n in r.notes if "failed" in n[0] and n[1] == "warning"]
    await m.close()


async def test_a_completed_tick_resets_the_failure_streak_and_only_it_counts_toward_max_ticks(tmp_path):
    fail, ok = RunResult(status="failed", error="x"), RunResult(status="completed", text="fine")
    c, db = clock(), make_db(tmp_path)
    r = FakeRunner([fail, fail, ok, fail, fail, ok, ok])
    m = LoopManager(db, r, c)
    row = m.create(session_id="s", prompt="p", interval="1m", max_ticks=3)
    await c.settle()
    await c.advance(60 * 12)
    loop = db.get("loops", row["id"])
    assert loop["state"] == "done" and "max ticks (3)" in loop["stop_reason"]
    assert loop["ticks"] == 3 and len(r.prompts) == 7  # 4 failed ticks did not count
    await m.close()


async def test_online_gate_also_waits_in_restart_storm_safe_mode(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    c = FakeClock()
    eng = AutomationEngine(server, clock=c, use_netwatch=False)
    server.background_paused = True  # run_prompt answers `failed` while this holds
    gate = asyncio.create_task(eng._online())
    await c.advance(60)
    assert not gate.done()
    server.background_paused = False
    await c.advance(5)
    await asyncio.wait_for(gate, 1)
    await server.close()


# ── 7: automations wait for the same gate as loops and jobs ───────────────────


async def test_model_actions_park_before_the_occurrence_is_consumed(tmp_path):
    c, db, r = clock(), make_db(tmp_path), FakeRunner()
    gate = asyncio.Event()

    async def wait_online():
        await gate.wait()

    m = AutomationManager(db, r, c, wait_online=wait_online)
    p = m.add(name="p", trigger={"type": "net_state"}, action={"type": "prompt", "prompt": "check"})
    n = m.add(name="n", trigger={"type": "net_state"}, action={"type": "notify", "text": "hi"})
    parked = asyncio.create_task(m.fire(p["id"], {"event": "cron"}))
    gone = asyncio.create_task(m.fire(p["id"], {"event": "cron"}))
    await c.settle()
    await m.fire(n["id"], {"event": "x"})  # no model needed: not held back
    assert r.notes and not r.prompts and db.get("automations", p["id"])["fire_count"] == 0
    await m.pause(p["id"])  # paused while parked: the occurrence is dropped, not run
    gate.set()
    await asyncio.gather(parked, gone)
    assert r.prompts == [] and db.get("automations", p["id"])["fire_count"] == 0
    await m.resume(p["id"])
    assert (await m.fire(p["id"], {"event": "cron"})).status == "completed" and len(r.prompts) == 1
    await m.stop()


async def test_engine_wires_the_gate_into_automations(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, ["ok"])
    c = FakeClock()
    eng = AutomationEngine(server, clock=c, use_netwatch=False)
    server.automation = eng
    await eng.start()
    row = eng.automations.add(
        name="p", trigger={"type": "net_state"}, action={"type": "prompt", "prompt": "x"}, cwd=str(tmp_path)
    )
    server.halt = Halt(reason="test", since=c.now())  # /daemon pause: used to consume the occurrence as `interrupted`
    task = asyncio.create_task(eng.automations.fire(row["id"], {"event": "e"}))
    await c.advance(30)
    assert not task.done() and provider.n == 0
    server.halt = None
    await c.advance(5)
    await until(lambda: task.done())
    assert task.result().status == "completed" and provider.n == 1
    await eng.stop()
    await server.close()


# ── 8: the idle trigger sees a busy user ──────────────────────────────────────


async def test_idle_activity_counts_an_attended_turn_in_flight_and_counts_from_its_end(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    c = FakeClock()
    eng = AutomationEngine(server, clock=c, use_netwatch=False)
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    server.last_user_activity = c.now() - 3 * 3600  # the last prompt was hours ago
    assert eng._user_activity() == server.last_user_activity
    live.streaming = True  # a long turn
    await c.advance(40 * 60)
    assert eng._user_activity() == c.now()
    live.streaming = False
    await c.advance(10 * 60)
    assert c.now() - eng._user_activity() == 10 * 60  # idle is counted from the turn's end, not the prompt
    live.background = True
    live.streaming = True  # an unattended run is not the user
    await c.advance(60)
    assert c.now() - eng._user_activity() == 11 * 60
    live.streaming = False
    await server.close()


# ── 9: long waits count suspend ──────────────────────────────────────────────


async def test_system_clock_sleep_ends_when_the_wall_clock_says_so(monkeypatch):
    """asyncio.sleep is monotonic and stops during suspend: after a suspend a day-long wait ended a day late."""
    jump = [0.0]
    fake_time = types.SimpleNamespace(time=lambda: time.time() + jump[0], monotonic=time.monotonic)
    monkeypatch.setattr(clock_mod, "time", fake_time)
    monkeypatch.setattr(clock_mod, "WALL_RECHECK_S", 0.02)
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, lambda: jump.__setitem__(0, 86400.0))  # the machine slept for a day
    started = time.monotonic()
    await asyncio.wait_for(SystemClock().sleep(86400.0 - 3600), 5)  # a 23 h wait
    assert time.monotonic() - started < 2
    await asyncio.wait_for(SystemClock().sleep(0.05), 2)  # ordinary short waits still work
    await asyncio.wait_for(SystemClock().sleep(0), 2)


# ── 10: cron parsing ─────────────────────────────────────────────────────────


def test_day_of_week_ranges_that_end_in_7_keep_sunday():
    assert parse_cron("0 9 * * 6-7").dow == frozenset({6, 0})
    assert parse_cron("0 9 * * 5-7").dow == frozenset({5, 6, 0})
    assert parse_cron("0 9 * * 7").dow == frozenset({0}) and parse_cron("0 9 * * 0-7").dow == frozenset(range(7))
    sat = ts(2026, 1, 3, 10)  # Saturday 10:00: the next 09:00 of a "weekend" job is Sunday
    assert cron_next("0 9 * * 6-7", sat, UTC) == ts(2026, 1, 4, 9)
    with pytest.raises(ScheduleError):
        parse_cron("0 9 * * 8")


def test_a_star_step_day_of_month_still_limits_the_day_like_vixie_cron():
    """`*/2` starts with `*`: day-of-month and day-of-week then both apply (Mondays on odd days), where it used to
    ignore the day-of-month and fire every Monday. Two plain lists stay "either" (the 1st or a Monday)."""
    assert cron_next("0 9 */2 * mon", ts(2026, 1, 5, 10), UTC) == ts(2026, 1, 19, 9)  # Jan 12 is a Monday, but even
    assert cron_next("0 9 */2 * mon", ts(2026, 1, 1, 10), UTC) == ts(2026, 1, 5, 9)
    assert cron_next("0 0 1 * 1", ts(2026, 1, 2), UTC) == ts(2026, 1, 5)
    assert cron_next("0 9 * * *", ts(2026, 1, 2, 10), UTC) == ts(2026, 1, 3, 9)


def test_intervals_shorter_than_a_minute_are_raised_to_the_floor():
    assert MIN_INTERVAL_S == PACING_MIN_S == 60
    for text in ("0.001s", "1s", "every 30 secs", "59s"):
        assert parse_interval(text) == 60 and parse_schedule(text).seconds == 60
    assert parse_interval("90s") == 90 and parse_interval("1m") == 60
    with pytest.raises(ScheduleError):
        parse_interval("0s")
    from k3code.automation.cronexpr import Schedule

    assert Schedule("interval", seconds=1).next_after(1000.0) == 1060.0  # a row stored before the floor existed


async def test_a_one_second_loop_is_a_one_minute_loop(tmp_path):
    c, db, r = clock(), make_db(tmp_path), FakeRunner()
    m = LoopManager(db, r, c)
    m.create(session_id="s", prompt="p", interval="1s")
    await c.settle()
    await c.advance(59)
    assert len(r.prompts) == 1
    await c.advance(2)
    assert len(r.prompts) == 2
    await m.close()
