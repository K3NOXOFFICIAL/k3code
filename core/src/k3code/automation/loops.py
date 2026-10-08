# Design ported from hermes-agent hermes_cli/loops.py (MIT); first-party implementation on the k3code gateway.
"""``/loop``: re-send a prompt into the same session on an interval (or self-paced) until it is done."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from k3code.automation.clock import Clock
from k3code.automation.cronexpr import Schedule, parse_schedule
from k3code.automation.runner import (
    LOOP_COMPLETE,
    Runner,
    RunResult,
    TickContext,
    WaitOnline,
    clamp_pacing,
    judge_condition,
)
from k3code.automation.store import AutomationDB, new_id

logger = logging.getLogger("k3code.automation.loops")

DEFAULT_MAX_TICKS = 50
SELF_PACED_DEFAULT_S = 300.0

_FOOTER_FIXED = (
    "\n\n[loop tick {n}] This prompt is re-sent on a schedule. When the task is fully complete, "
    f"end your reply with the single word {LOOP_COMPLETE}."
)
_FOOTER_PACED = (
    "\n\n[loop tick {n}, self-paced] Do one round of the task, then call the `schedule_next` tool with the number "
    "of seconds until the next tick (60-3600) and a short reason. When the task is fully complete, end your reply "
    f"with the single word {LOOP_COMPLETE} instead."
)


class LoopManager:
    def __init__(
        self,
        db: AutomationDB,
        runner: Runner,
        clock: Clock,
        *,
        wait_online: WaitOnline | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.db = db
        self.runner = runner
        self.clock = clock
        self.wait_online = wait_online
        self.on_change = on_change or (lambda: None)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # ── lifecycle ────────────────────────────────────────────────────

    def resume_all(self) -> int:
        """Daemon start: restart every active loop. A tick that came due while we were down fires once."""
        n = 0
        for row in self.db.rows("loops", "state='active'"):
            self._spawn(row["id"])
            n += 1
        return n

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()

    def _spawn(self, loop_id: str) -> None:
        old = self._tasks.get(loop_id)
        if old is not None and not old.done():
            return
        self._tasks[loop_id] = asyncio.get_running_loop().create_task(self._run(loop_id), name=f"loop-{loop_id}")

    # ── control ──────────────────────────────────────────────────────

    def create(
        self,
        *,
        session_id: str,
        prompt: str,
        interval: str | None = None,
        times: int | None = None,
        until: str | None = None,
        max_ticks: int = DEFAULT_MAX_TICKS,
        cwd: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        sched: Schedule | None = parse_schedule(interval) if interval else None
        now = self.clock.now()
        # Intervals and self-paced loops tick right away; cron-style ("daily 09:00") wait for the occurrence.
        first = sched.next_after(now) if sched is not None and sched.kind == "cron" else now
        loop_id = new_id()
        self.db.insert(
            "loops",
            id=loop_id,
            session_id=session_id,
            prompt=prompt,
            schedule=sched.to_dict() if sched else None,
            self_paced=0 if sched else 1,
            times=times,
            until_cond=until,
            max_ticks=max_ticks,
            next_run_at=first,
            created_at=now,
            cwd=cwd,
            model=model,
        )
        self._spawn(loop_id)
        self.on_change()
        return self.db.get("loops", loop_id) or {}

    def stop(self, ref: str | None = None, reason: str = "stopped by user", session_id: str | None = None) -> list[str]:
        """Stop one loop (id/prefix) or, with no ref, every active loop of ``session_id`` (or all)."""
        if ref:
            row = self.db.find("loops", ref)
            rows = [row] if row and row["state"] == "active" else []
        else:
            rows = self.db.rows("loops", "state='active'")
            if session_id:
                rows = [r for r in rows if r["session_id"] == session_id]
        for r in rows:
            self._finish(r["id"], "stopped", reason)
        return [r["id"] for r in rows]

    def list(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        return self.db.rows("loops", "state='active'" if active_only else "", order="created_at DESC")

    def active_count(self) -> int:
        return len(self.db.rows("loops", "state='active'"))

    def describe(self, row: dict[str, Any]) -> str:
        sched = Schedule.from_dict(row["schedule"]).describe() if row["schedule"] else "self-paced"
        cap = f"{row['ticks']}/{row['times']}" if row["times"] else f"{row['ticks']}/{row['max_ticks']} max"
        nxt = ""
        if row["state"] == "active" and row["next_run_at"]:
            nxt = f", next in {max(0, int(row['next_run_at'] - self.clock.now()))}s"
        until = f", until “{row['until_cond']}”" if row["until_cond"] else ""
        why = f" ({row['stop_reason']})" if row["stop_reason"] else ""
        return f"{row['id']}  {row['state']}{why}  {sched}  ticks {cap}{nxt}{until}  — {row['prompt'][:60]}"

    # ── the loop task ────────────────────────────────────────────────

    def _finish(self, loop_id: str, state: str, reason: str) -> None:
        self.db.update("loops", loop_id, state=state, stop_reason=reason, next_run_at=None)
        task = self._tasks.pop(loop_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        row = self.db.get("loops", loop_id)
        if row is not None and state != "stopped":
            self.runner.notify(f"Loop {loop_id} {state}: {reason}", "info", key=f"loop-{loop_id}")
        self.on_change()

    async def _run(self, loop_id: str) -> None:
        try:
            while True:
                row = self.db.get("loops", loop_id)
                if row is None or row["state"] != "active":
                    return
                delay = (row["next_run_at"] or 0) - self.clock.now()
                if delay > 0:
                    await self.clock.sleep(delay)
                    continue
                if self.wait_online is not None:
                    await self.wait_online()  # offline: defer; one tick fires when the network returns
                    row = self.db.get("loops", loop_id)
                    if row is None or row["state"] != "active":
                        return
                await self._tick(row)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("loop %s crashed", loop_id)
            self._finish(loop_id, "failed", f"internal error: {e}")

    async def _tick(self, row: dict[str, Any]) -> None:
        loop_id = row["id"]
        n = row["ticks"] + 1
        paced = bool(row["self_paced"])
        ctx = TickContext()
        prompt = row["prompt"] + (_FOOTER_PACED if paced else _FOOTER_FIXED).format(n=n)
        started = self.clock.now()
        run_id = self.db.insert(
            "job_runs", owner=loop_id, owner_kind="loop", started_at=started, scheduled_for=row["next_run_at"]
        )
        try:
            result: RunResult = await self.runner.run_prompt(
                prompt, session_id=row["session_id"], cwd=row["cwd"], model=row["model"], tick=ctx if paced else None,
                kind="loop_tick",
            )
        except asyncio.CancelledError:
            self.db.update("job_runs", run_id, status="interrupted", finished_at=self.clock.now())
            raise
        except Exception as e:  # noqa: BLE001
            result = RunResult(status="failed", error=str(e))
        now = self.clock.now()
        self.db.update(
            "job_runs",
            run_id,
            status=result.status,
            finished_at=now,
            error=result.error,
            api_calls=result.api_calls,
            summary=result.text[-300:],
            session_id=result.session_id,
        )
        self.db.update("loops", loop_id, ticks=n, last_run_at=now, last_result=result.text[-300:])
        self.on_change()

        current = self.db.get("loops", loop_id)
        if current is None or current["state"] != "active":
            return  # stopped while the tick ran
        if result.status in ("blocked", "needs_input"):
            self._finish(loop_id, "blocked", "needs input: the session is waiting for you")  # notifies once
            return
        if LOOP_COMPLETE in result.text:
            self._finish(loop_id, "done", f"model signalled {LOOP_COMPLETE}")
            return
        if row["times"] and n >= row["times"]:
            self._finish(loop_id, "done", f"ran {n} times")
            return
        if n >= row["max_ticks"]:
            self._finish(loop_id, "done", f"max ticks ({row['max_ticks']}) reached")
            return
        if row["until_cond"] and result.status == "completed":
            try:
                met, why = await judge_condition(self.runner, row["until_cond"], result.text)
            except Exception as e:  # noqa: BLE001 - a flaky judge must not kill the loop
                met, why = False, f"judge failed: {e}"
            if met:
                self._finish(loop_id, "done", f"condition met: {why}")
                return
        self.db.update("loops", loop_id, next_run_at=self._next_run(row, ctx, now))

    def _next_run(self, row: dict[str, Any], ctx: TickContext, now: float) -> float:
        # Always computed from *now*: a missed/deferred tick never triggers catch-up ticks.
        if row["self_paced"]:
            return now + (ctx.next_seconds if ctx.next_seconds is not None else clamp_pacing(SELF_PACED_DEFAULT_S))
        return Schedule.from_dict(row["schedule"]).next_after(now)
