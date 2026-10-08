import asyncio

from auto_helpers import FakeRunner, clock, make_db
from k3code.automation.loops import LoopManager
from k3code.automation.runner import LOOP_COMPLETE, RunResult, clamp_pacing


def mk(tmp_path, runner=None, **kw):
    c = clock()
    db = make_db(tmp_path)
    r = runner or FakeRunner()
    return c, db, r, LoopManager(db, r, c, **kw)


async def settle():
    for _ in range(40):
        await asyncio.sleep(0)


def test_clamp():
    assert clamp_pacing(5) == 60
    assert clamp_pacing(99999) == 3600
    assert clamp_pacing(600) == 600


async def test_interval_ticks_and_times(tmp_path):
    c, db, r, m = mk(tmp_path)
    row = m.create(session_id="s1", prompt="check build", interval="5m", times=3)
    await settle()
    assert len(r.prompts) == 1  # first tick immediately
    assert r.prompts[0]["session_id"] == "s1" and "check build" in r.prompts[0]["prompt"]
    await c.advance(299)
    assert len(r.prompts) == 1
    await c.advance(2)
    assert len(r.prompts) == 2
    await c.advance(300)
    assert len(r.prompts) == 3
    loop = db.get("loops", row["id"])
    assert loop["state"] == "done" and "3 times" in loop["stop_reason"]
    await c.advance(3000)
    assert len(r.prompts) == 3
    await m.close()


async def test_sentinel_ends_loop(tmp_path):
    r = FakeRunner([RunResult("completed", "working"), RunResult("completed", f"all done {LOOP_COMPLETE}")])
    c, db, r, m = mk(tmp_path, r)
    row = m.create(session_id="s", prompt="p", interval="1m")
    await settle()
    await c.advance(60)
    assert db.get("loops", row["id"])["state"] == "done"
    assert any("LOOP_COMPLETE" in n[0] for n in r.notes)
    await m.close()


async def test_max_ticks_backstop(tmp_path):
    c, db, r, m = mk(tmp_path)
    row = m.create(session_id="s", prompt="p", interval="1m", max_ticks=4)
    await settle()
    await c.advance(60 * 10)
    assert len(r.prompts) == 4
    assert "max ticks" in db.get("loops", row["id"])["stop_reason"]
    await m.close()


async def test_until_condition_uses_judge(tmp_path):
    r = FakeRunner(judge_replies=["NO: tests failing", "NO: still red", "YES: all green"])
    c, db, r, m = mk(tmp_path, r)
    row = m.create(session_id="s", prompt="p", interval="1m", until="tests pass")
    await settle()
    await c.advance(120)
    loop = db.get("loops", row["id"])
    assert loop["state"] == "done" and "all green" in loop["stop_reason"]
    assert len(r.prompts) == 3
    await m.close()


async def test_self_paced_clamped(tmp_path):
    r = FakeRunner()
    r.pace = [5, 99999, None]
    c, db, r, m = mk(tmp_path, r)
    row = m.create(session_id="s", prompt="p")  # no interval → self-paced
    await settle()
    assert "schedule_next" in r.prompts[0]["prompt"]
    assert db.get("loops", row["id"])["next_run_at"] == c.now() + 60  # 5s clamped up to 60
    t0 = c.now()
    await c.advance(61)
    assert len(r.prompts) == 2
    assert db.get("loops", row["id"])["next_run_at"] == t0 + 60 + 3600  # clamped down
    await c.advance(3601)
    assert len(r.prompts) == 3
    assert db.get("loops", row["id"])["next_run_at"] == t0 + 60 + 3600 + 300  # model forgot: default
    await m.close()


async def test_stop_and_list(tmp_path):
    c, db, r, m = mk(tmp_path)
    row = m.create(session_id="s", prompt="p", interval="1m")
    await settle()
    assert m.active_count() == 1
    assert m.stop(row["id"][:4]) == [row["id"]]
    assert m.active_count() == 0
    await c.advance(600)
    assert len(r.prompts) == 1
    assert m.list()[0]["state"] == "stopped"
    await m.close()


async def test_survives_restart_and_missed_tick_fires_once(tmp_path):
    c, db, r, m = mk(tmp_path)
    row = m.create(session_id="s", prompt="p", interval="5m")
    await settle()
    assert len(r.prompts) == 1
    await m.close()  # "daemon stops"
    c._now += 3600  # an hour passes while down: 12 intervals missed
    r2 = FakeRunner()
    m2 = LoopManager(db, r2, c)
    assert m2.resume_all() == 1
    await settle()
    assert len(r2.prompts) == 1  # fires exactly once, no catch-up
    await c.advance(299)
    assert len(r2.prompts) == 1
    await c.advance(2)
    assert len(r2.prompts) == 2
    assert db.get("loops", row["id"])["ticks"] == 3
    await m2.close()


async def test_offline_tick_deferred_then_fires_once(tmp_path):
    online = asyncio.Event()

    async def wait_online():
        await online.wait()

    c, db, r, m = mk(tmp_path, wait_online=wait_online)
    m.create(session_id="s", prompt="p", interval="1m")
    await settle()
    assert len(r.prompts) == 0  # deferred while offline
    await c.advance(600)  # ten intervals pass offline
    assert len(r.prompts) == 0
    online.set()
    await settle()
    assert len(r.prompts) == 1  # once, not ten times
    await c.advance(59)
    assert len(r.prompts) == 1
    await c.advance(2)
    assert len(r.prompts) == 2
    await m.close()


async def test_ticks_use_loop_tick_kind_and_until_judge_kind(tmp_path):
    c, db, r, m = mk(tmp_path, FakeRunner(judge_replies=["YES: ok"]))
    m.create(session_id="s1", prompt="check", interval="5m", until="build is green")
    await settle()
    assert r.prompts[0]["kind"] == "loop_tick"
    assert r.judge_kinds == ["goal_judge"]
