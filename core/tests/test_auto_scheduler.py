import asyncio

from auto_helpers import FakeRunner, clock, make_db
from k3code.automation.retry_policy import classify_failure, plan_unreachable_retry
from k3code.automation.runner import RunResult
from k3code.automation.scheduler import JobScheduler
from k3code.errors import AllProvidersUnreachable

UNREACH = RunResult(status="failed", error="all provider entries are unreachable", failure_kind="unreachable")
OK = RunResult(status="completed", text="done", api_calls=2)


def mk(tmp_path, results, **kw):
    c, db, r = clock(), make_db(tmp_path), FakeRunner(results)
    s = JobScheduler(db, r, c, **kw)
    s.start()
    return c, db, r, s


async def test_every_minute_two_runs_with_history(tmp_path):
    c, db, r, s = mk(tmp_path, [OK])
    job = s.add(prompt="say hi", schedule="* * * * *", name="hi", cwd="/tmp")
    await c.advance(61)
    await c.advance(60)
    runs = db.runs(job["id"])
    assert [x["status"] for x in runs] == ["completed", "completed"]
    assert r.prompts[0]["mode"] == "auto" and r.prompts[0]["cwd"] == "/tmp"
    assert db.get("jobs", job["id"])["run_count"] == 2
    assert any("cron “hi” completed" in n[0] for n in r.notes)
    await s.close()


async def test_a_restart_mid_run_does_not_rerun_the_job(tmp_path):
    """next_run_at only advanced when the run finished: a daemon restart mid-run found the job still due and ran it
    again at once."""
    c, db = clock(), make_db(tmp_path)
    release = asyncio.Event()

    class SlowRunner(FakeRunner):
        async def run_prompt(self, prompt, **kw):
            self.prompts.append({"prompt": prompt, **kw})
            await release.wait()
            return OK

    r = SlowRunner()
    s = JobScheduler(db, r, c)
    s.start()
    job = s.add(prompt="p", schedule="*/10 * * * *", name="j")
    due = db.get("jobs", job["id"])["next_run_at"]
    await c.advance(due - c.now() + 1)
    for _ in range(100):
        if r.prompts:
            break
        await asyncio.sleep(0.01)
    assert len(r.prompts) == 1 and db.get("jobs", job["id"])["next_run_at"] > c.now()  # advanced at start
    await s.close()  # the daemon dies mid-run
    r2 = FakeRunner([OK])
    s2 = JobScheduler(db, r2, c)
    s2.start()
    await c.advance(5)
    assert r2.prompts == []
    await s2.close()


async def test_pause_resume_rm_run_now(tmp_path):
    c, db, r, s = mk(tmp_path, [OK])
    job = s.add(prompt="p", schedule="5m", name="j")
    assert s.pause("j")
    await c.advance(1200)
    assert len(r.prompts) == 0
    assert s.resume(job["id"][:4])
    assert s.run_now("j")
    await c.advance(1)
    assert len(r.prompts) == 1  # manual run
    await c.advance(300)
    assert len(r.prompts) == 2
    assert s.remove("j") and s.find("j") is None
    await s.close()


async def test_unreachable_retry_ladder_5_15_30(tmp_path):
    c, db, r, s = mk(tmp_path, [UNREACH, UNREACH, UNREACH, UNREACH, OK])
    job = s.add(prompt="p", schedule="0 0 * * *", name="nightly")
    t0 = job["next_run_at"]
    await c.advance(t0 - c.now() + 1)  # first (failed) fire
    assert len(r.prompts) == 1
    j = db.get("jobs", job["id"])
    assert j["retry"]["attempt"] == 1 and j["next_run_at"] == t0 + 300
    assert db.runs(job["id"])[0]["status"] == "retrying"
    assert not any("failed" in n[0] for n in r.notes)  # notice suppressed while a retry is pending
    await c.advance(298)
    assert len(r.prompts) == 1
    await c.advance(2)
    assert len(r.prompts) == 2  # +5m
    await c.advance(900 + 1)
    assert len(r.prompts) == 3  # +15m
    await c.advance(1800 + 1)
    assert len(r.prompts) == 4  # +30m
    j = db.get("jobs", job["id"])
    assert j["retry"] is None  # exhausted → natural schedule
    assert j["next_run_at"] > c.now() + 3600
    assert any("failed" in n[0] for n in r.notes)
    await s.close()


async def test_retry_ladder_resets_after_reaching_model(tmp_path):
    c, db, r, s = mk(tmp_path, [UNREACH, OK])
    job = s.add(prompt="p", schedule="0 0 * * *", name="n")
    await c.advance(job["next_run_at"] - c.now() + 1)
    assert db.get("jobs", job["id"])["retry"]["attempt"] == 1
    await c.advance(301)
    assert db.get("jobs", job["id"])["retry"] is None
    await s.close()


def test_plan_ladder_and_natural_wins():
    assert plan_unreachable_retry(None, 0, 10_000)["at"] == 300
    assert plan_unreachable_retry({"attempt": 2}, 0, 10_000)["at"] == 1800
    assert plan_unreachable_retry({"attempt": 3}, 0, 10_000) is None
    assert plan_unreachable_retry(None, 0, 100) is None  # next natural fire comes before the rung


async def test_zero_api_call_guard(tmp_path):
    # network-looking failure *after* the model was reached must not use the ladder
    partial = RunResult(status="failed", error="connection reset", failure_kind="unreachable", api_calls=3)
    c, db, r, s = mk(tmp_path, [partial])
    job = s.add(prompt="p", schedule="0 0 * * *", name="n")
    await c.advance(job["next_run_at"] - c.now() + 1)
    assert db.get("jobs", job["id"])["retry"] is None
    await s.close()


async def test_quota_hold_parks_until_reset(tmp_path):
    quota = RunResult(
        status="failed", error="429 quota exhausted, retry after 7200s", failure_kind="quota", retry_after=7200
    )
    c, db, r, s = mk(tmp_path, [quota, OK])
    job = s.add(prompt="p", schedule="*/10 * * * *", name="q")
    await c.advance(job["next_run_at"] - c.now() + 1)
    assert len(r.prompts) == 1
    j = db.get("jobs", job["id"])
    assert j["quota_hold_until"] is not None and j["next_run_at"] >= c.now() + 7200
    assert db.runs(job["id"])[0]["status"] == "held"
    await c.advance(3600)
    assert len(r.prompts) == 1  # still parked: no probing
    await c.advance(3700)
    assert len(r.prompts) == 2
    assert db.get("jobs", job["id"])["quota_hold_until"] is None
    await s.close()


async def test_missed_run_within_grace_fires_once_outside_skipped(tmp_path):
    c, db, r, s = mk(tmp_path, [OK])
    job = s.add(prompt="p", schedule="*/5 * * * *", name="m")
    await s.close()  # daemon down
    c._now = job["next_run_at"] + 2 * 3600  # back after 2h: inside the 6h grace
    s2 = JobScheduler(db, r, c)
    s2.start()
    await c.advance(1)
    assert len(r.prompts) == 1  # once, not 24 times
    await c.advance(1)
    assert len(r.prompts) == 1
    await s2.close()
    # now a long outage: 10h > grace → skipped and logged
    c._now = db.get("jobs", job["id"])["next_run_at"] + 10 * 3600
    s3 = JobScheduler(db, r, c)
    s3.start()
    await c.advance(1)
    assert len(r.prompts) == 1
    assert db.runs(job["id"], 1)[0]["status"] == "skipped"
    assert "grace" in db.runs(job["id"], 1)[0]["note"]
    await s3.close()


async def test_offline_job_fires_once_on_recovery(tmp_path):
    online = asyncio.Event()

    async def wait_online():
        await online.wait()

    c, db, r, s = mk(tmp_path, [OK], wait_online=wait_online)
    job = s.add(prompt="p", schedule="* * * * *", name="o")
    await c.advance(600)
    assert len(r.prompts) == 0
    online.set()
    await c.advance(1)
    assert len(r.prompts) == 1
    await s.close()
    assert job


def test_classify_failure():
    assert classify_failure("x", AllProvidersUnreachable())[0] == "unreachable"
    assert classify_failure("HTTP 429: usage limit reached, retry after 3600s") == ("quota", 3600.0)
    assert classify_failure("Connection refused")[0] == "unreachable"
    assert classify_failure("syntax error in script")[0] == "other"


async def test_concurrency_slot_is_used(tmp_path):
    active = 0
    peak = 0
    sem = asyncio.Semaphore(1)

    class Slot:
        async def __aenter__(self):
            await sem.acquire()
            nonlocal active, peak
            active += 1
            peak = max(peak, active)

        async def __aexit__(self, *a):
            nonlocal active
            active -= 1
            sem.release()

    c, db, r, s = mk(tmp_path, [OK], slot=Slot)
    s.add(prompt="a", schedule="* * * * *", name="a")
    s.add(prompt="b", schedule="* * * * *", name="b")
    await c.advance(61)
    assert len(r.prompts) == 2 and peak == 1
    await s.close()
