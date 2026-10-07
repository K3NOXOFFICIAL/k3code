"""Automations: trigger → action, with a policy (cooldown, rate limit, overlap)."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Callable
from typing import Any

from k3code.automation.clock import Clock
from k3code.automation.cronexpr import Schedule, ScheduleError, parse_schedule
from k3code.automation.runner import Runner, RunResult
from k3code.automation.store import AutomationDB, new_id
from k3code.automation.triggers import (
    CronTrigger,
    FileChangeTrigger,
    GitTrigger,
    IdleTrigger,
    Trigger,
)
from k3code.automation.webhook import WebhookServer

logger = logging.getLogger("k3code.automation.automations")

TRIGGER_TYPES = ("cron", "file_change", "git", "webhook", "session_event", "net_state", "idle")
ACTION_TYPES = ("prompt", "shell", "notify", "goal")
SESSION_EVENTS = ("completed", "failed", "needs_input")
_TEMPLATE = re.compile(r"\{\{(\w+)\}\}")

#: Required keys per trigger / action type.
_REQUIRED = {
    "cron": ("schedule",),
    "file_change": ("glob",),
    "git": (),
    "webhook": (),
    "session_event": ("event",),
    "net_state": (),
    "idle": ("minutes",),
}
_ACTION_REQUIRED = {"prompt": "prompt", "shell": "command", "notify": "text", "goal": "objective"}


class AutomationError(ValueError):
    pass


def render(template: str, info: dict[str, Any]) -> str:
    """Substitute ``{{key}}`` from the trigger event (unknown keys are left as written)."""
    return _TEMPLATE.sub(lambda m: str(info.get(m.group(1), m.group(0))), template)


def validate(trigger: dict[str, Any], action: dict[str, Any]) -> None:
    t, a = trigger.get("type"), action.get("type")
    if t not in TRIGGER_TYPES:
        raise AutomationError(f"trigger.type must be one of {', '.join(TRIGGER_TYPES)} (got {t!r})")
    if a not in ACTION_TYPES:
        raise AutomationError(f"action.type must be one of {', '.join(ACTION_TYPES)} (got {a!r})")
    for key in _REQUIRED[t]:
        if trigger.get(key) in (None, ""):
            raise AutomationError(f"trigger {t} needs `{key}`")
    if not action.get(_ACTION_REQUIRED[a]):
        raise AutomationError(f"action {a} needs `{_ACTION_REQUIRED[a]}`")
    if t == "cron":
        try:
            parse_schedule(str(trigger["schedule"]))
        except ScheduleError as e:
            raise AutomationError(str(e)) from e
    if t == "session_event" and trigger["event"] not in SESSION_EVENTS:
        raise AutomationError(f"session_event.event must be one of {', '.join(SESSION_EVENTS)}")
    if t == "idle":
        try:
            if float(trigger["minutes"]) <= 0:
                raise ValueError
        except (TypeError, ValueError) as e:
            raise AutomationError("idle.minutes must be a positive number") from e


class AutomationManager:
    def __init__(
        self,
        db: AutomationDB,
        runner: Runner,
        clock: Clock,
        *,
        slot: Callable[[], Any] | None = None,
        activity: Callable[[], float] | None = None,
        on_change: Callable[[], None] | None = None,
        webhook_port: int | None = None,
        git_poll_s: float = 15.0,
        idle_poll_s: float = 30.0,
        grace_s: float = 6 * 3600,
    ) -> None:
        self.db, self.runner, self.clock = db, runner, clock
        import contextlib

        self.slot = slot or contextlib.nullcontext
        self.activity = activity or (lambda: clock.now())
        self.on_change = on_change or (lambda: None)
        self.webhook_port = webhook_port
        self.git_poll_s, self.idle_poll_s, self.grace_s = git_poll_s, idle_poll_s, grace_s
        self.webhook: WebhookServer | None = None
        self._triggers: dict[str, Trigger] = {}
        self._busy: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        if self.webhook_port is not None and self.webhook is None:
            self.webhook = WebhookServer(self.webhook_port)
            await self.webhook.start()
        for row in self.db.rows("automations", "state='active'"):
            self._arm(row)

    async def stop(self) -> None:
        for t in list(self._triggers.values()):
            await t.stop()
        self._triggers.clear()
        for task in list(self._tasks):
            task.cancel()
        if self.webhook is not None:
            await self.webhook.stop()
            self.webhook = None

    def _arm(self, row: dict[str, Any]) -> None:
        aid, trig = row["id"], row["trigger"]
        kind = trig["type"]

        async def fire(info: dict[str, Any]) -> None:
            await self.fire(aid, info)

        t: Trigger | None = None
        if kind == "cron":
            t = CronTrigger(
                trig,
                self._cron_fire(aid, fire),
                self.clock,
                cwd=row["cwd"],
                first_due=row["next_run_at"],
                grace_s=self.grace_s,
            )
        elif kind == "file_change":
            t = FileChangeTrigger(trig, fire, self.clock, cwd=row["cwd"])
        elif kind == "git":
            t = GitTrigger(trig, fire, self.clock, cwd=row["cwd"], poll_s=self.git_poll_s)
        elif kind == "idle":
            t = IdleTrigger(trig, fire, self.clock, cwd=row["cwd"], activity=self.activity, poll_s=self.idle_poll_s)
        elif kind == "webhook":
            if self.webhook is None:
                logger.warning("automation %s uses a webhook trigger but webhooks are disabled", aid)
            else:
                self.webhook.register(aid, str(trig.get("token") or ""), fire)
        if t is not None:
            self._triggers[aid] = t
            t.start()

    def _cron_fire(self, aid: str, fire: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        async def wrapped(info: dict[str, Any]) -> None:
            sched = parse_schedule(str(self.db.get("automations", aid)["trigger"]["schedule"]))  # type: ignore[index]
            self.db.update("automations", aid, next_run_at=sched.next_after(self.clock.now()))
            await fire(info)

        return wrapped

    async def _disarm(self, aid: str) -> None:
        t = self._triggers.pop(aid, None)
        if t is not None:
            await t.stop()
        if self.webhook is not None:
            self.webhook.unregister(aid)

    # ── CRUD ─────────────────────────────────────────────────────────

    def add(
        self,
        *,
        name: str,
        trigger: dict[str, Any],
        action: dict[str, Any],
        policy: dict[str, Any] | None = None,
        cwd: str = "",
    ) -> dict[str, Any]:
        validate(trigger, action)
        trigger = dict(trigger)
        if trigger["type"] == "webhook":
            if self.webhook_port is None:
                raise AutomationError("webhook triggers are disabled: set automation.webhook_port in config.yaml")
            trigger.setdefault("token", secrets.token_urlsafe(24))
        now = self.clock.now()
        aid = new_id()
        nxt = Schedule.next_after(parse_schedule(str(trigger["schedule"])), now) if trigger["type"] == "cron" else None
        self.db.insert(
            "automations",
            id=aid,
            name=name or f"{trigger['type']}→{action['type']}",
            trigger=trigger,
            action=dict(action),
            policy=policy or {},
            created_at=now,
            next_run_at=nxt,
            cwd=cwd,
        )
        row = self.db.get("automations", aid) or {}
        self._arm(row)
        self.on_change()
        return row

    def find(self, ref: str) -> dict[str, Any] | None:
        row = self.db.find("automations", ref)
        if row is None:
            named = [r for r in self.db.rows("automations") if r["name"] == ref]
            row = named[0] if len(named) == 1 else None
        return row

    async def remove(self, ref: str) -> bool:
        row = self.find(ref)
        if row is None:
            return False
        await self._disarm(row["id"])
        self.db.delete("automations", row["id"])
        self.on_change()
        return True

    async def pause(self, ref: str) -> bool:
        row = self.find(ref)
        if row is None:
            return False
        await self._disarm(row["id"])
        self.db.update("automations", row["id"], state="paused")
        self.on_change()
        return True

    async def resume(self, ref: str) -> bool:
        row = self.find(ref)
        if row is None:
            return False
        if row["trigger"]["type"] == "cron":  # a paused cron automation does not "miss" runs
            self.db.update(
                "automations",
                row["id"],
                next_run_at=parse_schedule(row["trigger"]["schedule"]).next_after(self.clock.now()),
            )
        self.db.update("automations", row["id"], state="active")
        await self._disarm(row["id"])
        self._arm(self.db.get("automations", row["id"]) or {})
        self.on_change()
        return True

    def active_count(self) -> int:
        return len(self.db.rows("automations", "state='active'"))

    def describe(self, row: dict[str, Any]) -> str:
        trig = {k: v for k, v in row["trigger"].items() if k not in ("type", "token")}
        act = row["action"]
        detail = next((str(act[k])[:40] for k in ("prompt", "command", "text", "objective") if k in act), "")
        last = f", fired {row['fire_count']}x" if row["fire_count"] else ""
        return (
            f"{row['id']}  {row['name']}  [{row['state']}]  {row['trigger']['type']} {trig or ''} → "
            f"{act['type']} “{detail}”{last}"
        )

    # ── firing ───────────────────────────────────────────────────────

    async def fire(self, aid: str, info: dict[str, Any], *, force: bool = False) -> RunResult | None:
        row = self.db.get("automations", aid)
        if row is None or (row["state"] != "active" and not force):
            return None
        now = self.clock.now()
        policy = row["policy"] or {}
        if not force:
            cd = float(policy.get("cooldown_s") or 0)
            if cd and row["last_fired_at"] and now - row["last_fired_at"] < cd:
                logger.info("automation %s: skipped (cooldown)", aid)
                return None
            per_hour = int(policy.get("max_per_hour") or 0)
            if per_hour:
                recent = self.db.rows("job_runs", "owner=? AND started_at>?", (aid, now - 3600))
                if len(recent) >= per_hour:
                    logger.info("automation %s: skipped (rate limit)", aid)
                    return None
        if aid in self._busy:
            logger.info("automation %s: skipped (previous run still going)", aid)
            return None
        self._busy.add(aid)
        try:  # everything after the claim sits in the try: an exception in the inserts leaked `_busy` for good
            run_id = self.db.insert(
                "job_runs",
                owner=aid,
                owner_kind="automation",
                started_at=now,
                scheduled_for=now,
                note=str(info.get("event", "")),
            )
            self.db.update("automations", aid, last_fired_at=now, fire_count=row["fire_count"] + 1)
            self.on_change()
            info = {**info, "automation": row["name"]}
            try:
                result = await self._run_action(row, info)
            except asyncio.CancelledError:
                self.db.update("job_runs", run_id, status="interrupted", finished_at=self.clock.now())
                raise
            except Exception as e:  # noqa: BLE001
                result = RunResult(status="failed", error=str(e))
        finally:
            self._busy.discard(aid)
        self.db.update(
            "job_runs",
            run_id,
            status=result.status,
            finished_at=self.clock.now(),
            api_calls=result.api_calls,
            error=result.error,
            summary=result.text[-300:],
            session_id=result.session_id,
        )
        if result.status == "failed":
            self.runner.notify(f"automation “{row['name']}” failed: {result.error[:120]}", "warning", key=f"auto-{aid}")
        self.on_change()
        return result

    async def _run_action(self, row: dict[str, Any], info: dict[str, Any]) -> RunResult:
        act = row["action"]
        cwd = row["cwd"] or str(act.get("cwd") or "")
        kind = act["type"]
        if kind == "notify":
            self.runner.notify(render(str(act["text"]), info), str(act.get("level") or "info"), key=f"auto-{row['id']}")
            return RunResult(status="completed", text="notified")
        async with self.slot():
            if kind == "shell":
                rc, out = await self.runner.run_shell(render(str(act["command"]), info), cwd)
                return RunResult(
                    status="completed" if rc == 0 else "failed", text=out[-2000:], error="" if rc == 0 else f"exit {rc}"
                )
            if kind == "goal":
                return await self.runner.start_goal(render(str(act["objective"]), info), None, cwd)
            sess = act.get("session")
            return await self.runner.run_prompt(
                render(str(act["prompt"]), info),
                session_id=None if sess in (None, "", "new") else str(sess),
                cwd=cwd,
                model=str(act.get("model") or ""),
                name=f"auto: {row['name']}",
                mode=str(act.get("mode") or "auto"),
            )

    async def test(self, ref: str) -> str:
        """Fire now, ignoring policy and state; returns a one-line outcome."""
        row = self.find(ref)
        if row is None:
            return f"No such automation: {ref}"
        res = await self.fire(row["id"], {"event": "test"}, force=True)
        if res is None:
            return "Skipped (already running)."
        return f"Test run {res.status}: {(res.text or res.error).strip()[:200]}"

    # ── pushed events ────────────────────────────────────────────────

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def session_event(self, session_id: str, status: str, origin: str = "") -> None:
        """A session's run ended (``status``: completed | failed | needs_input)."""
        for row in self.db.rows("automations", "state='active'"):
            trig = row["trigger"]
            if trig["type"] != "session_event" or trig["event"] != status:
                continue
            if origin and not trig.get("include_automation"):
                continue  # never trigger on our own background runs (feedback loops)
            if trig.get("session") and trig["session"] != session_id:
                continue
            self._spawn(self.fire(row["id"], {"event": f"session_{status}", "session": session_id, "status": status}))

    def net_change(self, was_usable: bool, now_usable: bool) -> None:
        if was_usable or not now_usable:
            return
        for row in self.db.rows("automations", "state='active'"):
            if row["trigger"]["type"] == "net_state":
                self._spawn(self.fire(row["id"], {"event": "net_online"}))
