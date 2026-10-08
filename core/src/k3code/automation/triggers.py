"""Event triggers. Each trigger is started with a callback ``fire(info: dict)`` and stopped on pause/rm/shutdown.

* ``cron``: clock-driven, reuses :mod:`cronexpr` (missed-run-once semantics).
* ``file_change``: glob + debounce on inotify (``watchfiles``).
* ``git``: poll ``git rev-parse`` for a new commit on a branch, or a checkout (HEAD moved to another ref).
* ``idle``: no user activity for N minutes.
* ``webhook`` lives in :mod:`webhook`; ``session_event`` / ``net_state`` are pushed in by the engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from k3code.automation.clock import Clock
from k3code.automation.cronexpr import Schedule

logger = logging.getLogger("k3code.automation.triggers")

Fire = Callable[[dict[str, Any]], Awaitable[None]]


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``**`` crosses directories, ``*`` and ``?`` do not; matched against a ``/``-separated relative path."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile(out + r"\Z")


class Trigger:
    RESTART_BASE_S = 1.0  # first back-off after a crash (doubles up to 60 s)

    def __init__(self, spec: dict[str, Any], fire: Fire, clock: Clock, *, cwd: str = "") -> None:
        self.spec, self.fire, self.clock, self.cwd = spec, fire, clock, cwd
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._guarded(), name=f"trigger-{self.spec.get('type')}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _guarded(self) -> None:
        """Run the trigger; when it crashes or ends early, start it again with back-off (1 s .. 60 s). A trigger task
        used to end for good on the first exception (a repo dir missing for a moment) while the automation's row
        stayed 'active' and silently never fired again."""
        delay = self.RESTART_BASE_S
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("trigger %s crashed; restarting in %.0fs", self.spec, delay)
            if time.monotonic() - started > 120:
                delay = self.RESTART_BASE_S  # it ran fine for a while: the next failure starts the ladder over
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, 60.0)

    async def run(self) -> None:
        raise NotImplementedError


class CronTrigger(Trigger):
    """Fires on a schedule; ``first_due`` (persisted by the manager) lets a missed run fire once after downtime."""

    def __init__(self, *a: Any, first_due: float | None = None, grace_s: float = 6 * 3600, **kw: Any) -> None:
        super().__init__(*a, **kw)
        from k3code.automation.cronexpr import parse_schedule

        self.schedule: Schedule = parse_schedule(str(self.spec["schedule"]))
        self.first_due = first_due
        self.grace_s = grace_s
        self.next_run: float | None = None

    async def run(self) -> None:
        now = self.clock.now()
        due = self.first_due if self.first_due is not None else self.schedule.next_after(now)
        if due <= now and now - due > self.grace_s:
            logger.warning("cron automation missed its run by >%ss: skipped", self.grace_s)
            due = self.schedule.next_after(now)
        while True:
            self.next_run = due
            wait = due - self.clock.now()
            if wait > 0:
                await self.clock.sleep(wait)
                continue
            await self.fire({"event": "cron", "scheduled_for": due})
            due = self.schedule.next_after(self.clock.now())  # from now: never catch up


class FileChangeTrigger(Trigger):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.root = Path(self.spec.get("path") or self.cwd or ".").expanduser().resolve()
        self.pattern = glob_to_regex(str(self.spec.get("glob") or "**/*"))
        self.debounce_ms = int(float(self.spec.get("debounce", 2.0)) * 1000)
        self.ready = asyncio.Event()

    async def run(self) -> None:
        from watchfiles import awatch

        if not self.root.is_dir():
            logger.warning("file_change: %s is not a directory", self.root)
            return
        self.ready.set()
        async for changes in awatch(
            self.root,
            debounce=self.debounce_ms,
            step=min(50, self.debounce_ms),
            stop_event=self._stop,
            rust_timeout=200,
            yield_on_timeout=False,
        ):
            paths = []
            for _kind, p in changes:
                try:
                    rel = Path(p).resolve().relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if self.pattern.match(rel):
                    paths.append(rel)
            if paths:
                await self.fire({"event": "file_change", "path": paths[0], "paths": ", ".join(sorted(set(paths))[:20])})


async def git_out(cwd: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd or None, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip() if proc.returncode == 0 else ""


class GitTrigger(Trigger):
    """``event: commit`` (default) fires when ``branch`` (or HEAD) points at a new commit; ``checkout`` when the
    checked-out ref changes."""

    def __init__(self, *a: Any, poll_s: float = 15.0, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.repo = str(Path(self.spec.get("repo") or self.cwd or ".").expanduser())
        self.event = str(self.spec.get("event") or "commit")
        self.branch = self.spec.get("branch")
        self.poll_s = float(self.spec.get("poll", poll_s))
        self.last: str | None = None

    async def probe(self) -> str:
        if self.event == "checkout":
            return await git_out(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        return await git_out(self.repo, "rev-parse", str(self.branch or "HEAD"))

    async def run(self) -> None:
        self.last = await self.probe()
        while True:
            await self.clock.sleep(self.poll_s)
            cur = await self.probe()
            if cur and self.last is not None and cur != self.last:
                prev, self.last = self.last, cur
                info: dict[str, Any] = {
                    "event": f"git_{self.event}",
                    "commit": cur,
                    "previous": prev,
                    "repo": self.repo,
                }
                if self.event == "commit":
                    info["subject"] = await git_out(self.repo, "log", "-1", "--format=%s", cur)
                else:
                    info["branch"] = cur
                await self.fire(info)
            elif cur:
                self.last = cur


class IdleTrigger(Trigger):
    """Fires once after N minutes without user activity; re-arms when activity resumes."""

    def __init__(self, *a: Any, activity: Callable[[], float], poll_s: float = 30.0, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.activity = activity
        self.minutes = float(self.spec.get("minutes", 30))
        self.poll_s = float(self.spec.get("poll", poll_s))
        self.armed = True

    async def run(self) -> None:
        while True:
            await self.clock.sleep(self.poll_s)
            idle = self.clock.now() - self.activity()
            if idle >= self.minutes * 60:
                if self.armed:
                    self.armed = False
                    await self.fire({"event": "idle", "idle_minutes": int(idle // 60)})
            else:
                self.armed = True
