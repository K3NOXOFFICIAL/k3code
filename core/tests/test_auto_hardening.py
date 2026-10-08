"""Automation supervision, /stop handling and retention (found by the long-run audit)."""

from __future__ import annotations

import asyncio

import pytest

from auto_helpers import FakeRunner, clock, make_db
from k3code.automation.automations import AutomationManager
from k3code.automation.scheduler import JobScheduler
from k3code.automation.triggers import Trigger
from m1cmd_helpers import make_server, new_session


async def test_stopping_an_unattended_turn_does_not_kill_the_caller(tmp_path, monkeypatch):
    """/stop cancels the turn task; run_prompt re-raised that CancelledError into its caller, which every supervisor
    (cron scheduler, loop, automation) treats as its own cancellation: next_run_at never advanced, so a cron job
    re-fired at once, and loops died while their row stayed 'active'."""
    from k3code.automation.server_runner import ServerRunner

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sid = await new_session(server, tmp_path)
    started = asyncio.Event()
    real = server._run_turn_locked

    async def hold(session, text):
        started.set()
        await asyncio.sleep(60)
        return await real(session, text)

    server._run_turn_locked = hold  # type: ignore[method-assign]
    runner = ServerRunner(server)
    caller = asyncio.create_task(runner.run_prompt("tick", session_id=sid))
    await asyncio.wait_for(started.wait(), 10)
    assert await server.interrupt_turn(sid)
    result = await asyncio.wait_for(caller, 10)  # returns, instead of raising CancelledError
    assert result.status == "interrupted"
    # ...whereas cancelling the caller itself still propagates and takes the turn down with it
    started.clear()
    caller2 = asyncio.create_task(runner.run_prompt("tick 2", session_id=sid))
    await asyncio.wait_for(started.wait(), 10)
    caller2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller2


async def test_scheduler_loop_survives_a_failing_pass(tmp_path, monkeypatch):
    c, db, r = clock(), make_db(tmp_path), FakeRunner()
    s = JobScheduler(db, r, c)
    real_rows = db.rows
    calls = {"n": 0}

    def flaky(table, *a, **k):
        if table == "jobs" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("database is locked")
        return real_rows(table, *a, **k)

    monkeypatch.setattr(db, "rows", flaky)
    s.start()
    await asyncio.sleep(0.05)
    assert s._main is not None and not s._main.done(), "one failing pass ended the scheduler task for good"
    await s.close()


async def test_a_crashed_trigger_is_restarted(tmp_path):
    class Flaky(Trigger):
        RESTART_BASE_S = 0.01
        runs = 0

        async def run(self) -> None:
            Flaky.runs += 1
            if Flaky.runs == 1:
                raise FileNotFoundError("repo dir is missing for a moment")
            await self._stop.wait()

    t = Flaky({"type": "flaky"}, lambda *a, **k: None, clock())
    t.start()
    for _ in range(100):
        if Flaky.runs >= 2:
            break
        await asyncio.sleep(0.01)
    assert Flaky.runs == 2
    await t.stop()


async def test_fire_releases_its_claim_even_when_bookkeeping_fails(tmp_path, monkeypatch):
    c, db, r = clock(), make_db(tmp_path), FakeRunner()
    m = AutomationManager(db, r, c)
    a = m.add(name="n", trigger={"type": "net_state"}, action={"type": "notify", "text": "x"})
    real_insert = db.insert
    boom = {"on": True}

    def insert(table, **fields):
        if boom["on"] and table == "job_runs":
            boom["on"] = False
            raise RuntimeError("disk full")
        return real_insert(table, **fields)

    monkeypatch.setattr(db, "insert", insert)
    with pytest.raises(RuntimeError):
        await m.fire(a["id"], {"event": "net"}, force=True)
    assert a["id"] not in m._busy, "an exception after the claim left the automation 'still running' forever"
    assert await m.fire(a["id"], {"event": "net"}, force=True) is not None


def test_run_history_is_pruned_and_stale_runs_are_reconciled(tmp_path):
    db = make_db(tmp_path)
    now = 1_000_000_000.0
    day = 86400.0
    db.insert("jobs", id="j1", name="job", prompt="p", schedule="* * * * *", created_at=now)
    for i in range(300):  # a minutely job for ~5 hours long ago
        db.insert(
            "job_runs",
            owner="j1",
            owner_kind="job",
            started_at=now - 60 * day + i * 60,
            status="completed",
            session_id=f"s{i}",
        )
    for i in range(5):
        db.insert(
            "job_runs",
            owner="j1",
            owner_kind="job",
            started_at=now - i * 3600,
            status="completed",
            session_id=f"new{i}",
        )
    db.insert("job_runs", owner="gone", owner_kind="job", started_at=now - 3600, status="completed", session_id="orph")
    removed = db.prune_runs(now, max_age_days=30, keep_per_owner=50)
    assert "orph" in removed  # the owner was deleted: its history goes regardless of age
    left = db.rows("job_runs", "owner='j1'", order="id")
    assert len(left) == 50  # the newest 50 of the owner (5 recent + 45 old); the other 255 old ones are gone
    assert all(r["session_id"].startswith("new") or int(r["session_id"][1:]) >= 255 for r in left)
    db.insert("job_runs", owner="j1", owner_kind="job", started_at=now, status="running")
    assert db.interrupt_stale_runs(now + 5) == 1
    assert db.rows("job_runs", "status='running'") == []


async def test_engine_prune_deletes_finished_unattended_sessions_only(tmp_path, monkeypatch):
    from k3code.automation.engine import AutomationEngine

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    eng = AutomationEngine(server)
    server.automation = eng
    old = server.store.create(title="cron run", model="m", cwd=str(tmp_path))
    old.meta["origin"] = "automation"
    server.store.save(old)
    mine = server.store.create(title="my work", model="m", cwd=str(tmp_path))
    eng.db.insert("jobs", id="gone-job-owner", name="x", prompt="p", schedule="* * * * *", created_at=0.0)
    eng.db.delete("jobs", "gone-job-owner")  # the job was removed: its runs are orphans
    for sid in (old.session_id, mine.session_id):
        eng.db.insert(
            "job_runs",
            owner="gone-job-owner",
            owner_kind="job",
            started_at=eng.clock.now(),
            status="completed",
            session_id=sid,
        )
    assert eng.prune() == 1
    assert server.store.get(old.session_id) is None and server.store.get(mine.session_id) is not None
    # and the session to continue is never an unattended one
    assert server.store.most_recent().session_id == mine.session_id
    await server.close()
