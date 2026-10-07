"""``/schedule``: cron jobs that run in the daemon as background sessions, with the reliability rules."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from k3code.automation.clock import Clock
from k3code.automation.cronexpr import Schedule, parse_schedule
from k3code.automation.retry_policy import (
    plan_unreachable_retry,
    quota_boundary,
)
from k3code.automation.runner import Runner, RunResult, WaitOnline
from k3code.automation.store import AutomationDB, new_id

logger = logging.getLogger("k3code.automation.scheduler")

DEFAULT_GRACE_S = 6 * 3600.0
POLL_S = 5.0  # re-read the db at least this often (the CLI edits it from another process)


class JobScheduler:
    def __init__(
        self,
        db: AutomationDB,
        runner: Runner,
        clock: Clock,
        *,
        wait_online: WaitOnline | None = None,
        slot: Callable[[], Any] | None = None,
        on_change: Callable[[], None] | None = None,
        on_fire: Callable[[dict[str, Any], RunResult], None] | None = None,
        grace_s: float = DEFAULT_GRACE_S,
    ) -> None:
        self.db = db
        self.runner = runner
        self.clock = clock
        self.wait_online = wait_online
        self.slot = slot or contextlib.nullcontext
        self.on_change = on_change or (lambda: None)
        self.on_fire = on_fire
        self.grace_s = grace_s
        self._wake = asyncio.Event()
        self._running: dict[str, asyncio.Task[None]] = {}
        self._main: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._main is None:
            self._main = asyncio.get_running_loop().create_task(self._loop(), name="job-scheduler")

    async def close(self) -> None:
        tasks = [t for t in [self._main, *self._running.values()] if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._main = None
        self._running.clear()

    def wake(self) -> None:
        self._wake.set()

    # ── job CRUD ─────────────────────────────────────────────────────

    def add(self, *, prompt: str, schedule: str | Schedule, name: str = "", cwd: str = "", model: str = "",
            grace_s: float | None = None) -> dict[str, Any]:
        sched = parse_schedule(schedule) if isinstance(schedule, str) else schedule
        now = self.clock.now()
        job_id = new_id()
        self.db.insert(
            "jobs", id=job_id, name=name or prompt[:40], prompt=prompt, schedule=sched.to_dict(), cwd=cwd, model=model,
            next_run_at=sched.next_after(now), created_at=now, grace_s=grace_s if grace_s is not None else self.grace_s,
        )
        self.wake()
        self.on_change()
        return self.db.get("jobs", job_id) or {}

    def find(self, ref: str) -> dict[str, Any] | None:
        row = self.db.find("jobs", ref)
        if row is None:
            rows = [r for r in self.db.rows("jobs") if r["name"] == ref]
            row = rows[0] if len(rows) == 1 else None
        return row

    def remove(self, ref: str) -> bool:
        row = self.find(ref)
        if row is None:
            return False
        task = self._running.pop(row["id"], None)
        if task:
            task.cancel()
        self.db.delete("jobs", row["id"])
        self.on_change()
        return True

    def pause(self, ref: str) -> bool:
        row = self.find(ref)
        if row is None:
            return False
        self.db.update("jobs", row["id"], state="paused", next_run_at=None, retry=None)
        self.on_change()
        return True

    def resume(self, ref: str) -> bool:
        row = self.find(ref)
        if row is None:
            return False
        sched = Schedule.from_dict(row["schedule"])
        self.db.update("jobs", row["id"], state="active", next_run_at=sched.next_after(self.clock.now()))
        self.wake()
        self.on_change()
        return True

    def run_now(self, ref: str) -> bool:
        row = self.find(ref)
        if row is None:
            return False
        self.db.update("jobs", row["id"], run_requested=1)
        self.wake()
        return True

    def active_count(self) -> int:
        return len(self.db.rows("jobs", "state='active'"))

    # ── main loop ────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            now = self.clock.now()
            soonest = POLL_S
            for job in self.db.rows("jobs", "state='active'"):
                if job["id"] in self._running:
                    continue
                manual = bool(job["run_requested"])
                due_at = job["next_run_at"]
                if manual or (due_at is not None and due_at <= now):
                    self._running[job["id"]] = asyncio.get_running_loop().create_task(
                        self._execute(job["id"], manual), name=f"job-{job['id']}"
                    )
                elif due_at is not None:
                    soonest = min(soonest, due_at - now)
            sleeper = asyncio.ensure_future(self.clock.sleep(max(0.0, soonest)))
            waker = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                sleeper.cancel()
                waker.cancel()
                # Wait for the cancellations so a fake-clock sleeper never lingers.
                await asyncio.gather(sleeper, waker, return_exceptions=True)

    async def _execute(self, job_id: str, manual: bool) -> None:
        try:
            await self._execute_inner(job_id, manual)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("job %s crashed", job_id)
        finally:
            self._running.pop(job_id, None)
            self.wake()

    async def _execute_inner(self, job_id: str, manual: bool) -> None:
        job = self.db.get("jobs", job_id)
        if job is None:
            return
        sched = Schedule.from_dict(job["schedule"])
        if self.wait_online is not None:
            await self.wait_online()  # offline: parked here; fires once on recovery (or is skipped below)
            job = self.db.get("jobs", job_id)
            if job is None or job["state"] != "active":
                return
        now = self.clock.now()
        scheduled_for = job["next_run_at"]
        if manual:
            self.db.update("jobs", job_id, run_requested=0)
        elif scheduled_for is not None and now - scheduled_for > job["grace_s"]:
            late_h = (now - scheduled_for) / 3600
            logger.warning("job %s missed its run by %.1fh (> grace): skipped", job_id, late_h)
            self.db.insert(
                "job_runs", owner=job_id, started_at=now, finished_at=now, status="skipped",
                scheduled_for=scheduled_for, note=f"missed by {late_h:.1f}h, outside the grace window",
            )
            self.db.update("jobs", job_id, next_run_at=sched.next_after(now), last_status="skipped")
            self.on_change()
            return
        run_id = self.db.insert("job_runs", owner=job_id, started_at=now, scheduled_for=scheduled_for, status="running")
        self.on_change()
        try:
            async with self.slot():
                result = await self.runner.run_prompt(
                    job["prompt"], session_id=None, cwd=job["cwd"], model=job["model"], name=f"cron: {job['name']}", mode="auto"
                )
        except asyncio.CancelledError:
            self.db.update("job_runs", run_id, status="interrupted", finished_at=self.clock.now())
            raise
        except Exception as e:  # noqa: BLE001
            result = RunResult(status="failed", error=str(e), failure_kind="other")
        self._record(job, run_id, result, sched, manual)

    def _record(self, job: dict[str, Any], run_id: int, result: RunResult, sched: Schedule, manual: bool) -> None:
        now = self.clock.now()
        fields: dict[str, Any] = {"run_count": job["run_count"] + 1, "last_status": result.status}
        natural = sched.next_after(now)
        status, note, quiet = result.status, "", False
        if manual and (job["next_run_at"] or 0) > now:
            nxt = job["next_run_at"]  # manual run leaves the schedule alone
        else:
            nxt = natural
        if result.status == "failed" and result.failure_kind == "unreachable" and result.api_calls == 0:
            plan = plan_unreachable_retry(job["retry"], now, natural)
            if plan is not None:
                fields["retry"] = plan
                nxt, status, quiet = plan["at"], "retrying", True
                note = f"network unreachable (0 API calls): retry {plan['attempt']}/3 in {int(plan['at'] - now)}s"
            else:
                fields["retry"] = None
                note = "network unreachable; retry ladder exhausted, waiting for the next occurrence"
        else:
            if result.api_calls > 0 or result.status == "completed":
                fields["retry"] = None  # reached the model: the ladder resets
            if result.status == "failed" and result.failure_kind == "quota" and result.retry_after:
                nxt = quota_boundary(now, result.retry_after)
                fields["quota_hold_until"] = nxt
                status, note = "held", f"provider quota hold until {int(result.retry_after)}s from now"
            else:
                fields["quota_hold_until"] = None
        fields["next_run_at"] = nxt
        self.db.update("jobs", job["id"], **fields)
        self.db.update(
            "job_runs", run_id, status=status, finished_at=now, api_calls=result.api_calls, error=result.error,
            summary=result.text[-300:], session_id=result.session_id, note=note,
        )
        if not quiet:
            body = result.text.strip().splitlines()[-1][:160] if result.text.strip() else result.error[:160]
            self.runner.notify(f"cron “{job['name']}” {status}: {body}", "info" if status == "completed" else "warning",
                               key=f"job-{job['id']}")
        if self.on_fire is not None:
            self.on_fire(job, result)
        self.on_change()
