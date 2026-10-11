"""The automation engine: owns loops, the job scheduler, automations and suggestions inside the daemon."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from k3code.automation.automations import AutomationManager
from k3code.automation.clock import Clock, SystemClock
from k3code.automation.loops import LoopManager
from k3code.automation.runner import Runner, WaitOnline
from k3code.automation.scheduler import JobScheduler
from k3code.automation.server_runner import ServerRunner
from k3code.automation.store import AutomationDB
from k3code.automation.suggestions import Suggestions
from k3code.paths import ensure_private_dir
from k3code.reliability.governor import Governor, GovernorConfig

if TYPE_CHECKING:
    from k3code.gateway.server import GatewayServer

logger = logging.getLogger("k3code.automation")

HALT_POLL_S = 5.0
LOCK_NAME = "automation.lock"  # under <home>/run: whoever holds it runs the loops, jobs and triggers
LOCK_RETRY_S = 30.0  # a process that lost the lock race tries again this often (the holder may have exited)


class AutomationEngine:
    def __init__(
        self,
        server: GatewayServer,
        *,
        clock: Clock | None = None,
        db: AutomationDB | None = None,
        runner: Runner | None = None,
        wait_online: WaitOnline | None = None,
        use_netwatch: bool = True,
    ) -> None:
        self.server = server
        self.clock = clock or SystemClock()
        home = Path(server._home())
        self.db = db or AutomationDB(home / "automation.db")
        self.runner: Runner = runner or ServerRunner(server)
        self._wait_online = wait_online
        self._use_netwatch = use_netwatch and wait_online is None
        self._rel: Any = None
        cfg = server.config.automation
        self.governor = Governor(GovernorConfig(max_total=max(1, cfg.max_concurrent), home=home))
        self.loops = LoopManager(self.db, self.runner, self.clock, wait_online=self._online, on_change=self.changed)
        self.jobs = JobScheduler(
            self.db,
            self.runner,
            self.clock,
            wait_online=self._online,
            slot=lambda: self.governor.slot("llm"),
            on_change=self.changed,
            grace_s=cfg.grace_hours * 3600,
        )
        self.automations = AutomationManager(
            self.db,
            self.runner,
            self.clock,
            slot=lambda: self.governor.slot("llm"),
            activity=self._user_activity,
            on_change=self.changed,
            wait_online=self._online,
            webhook_port=cfg.webhook_port,
            git_poll_s=cfg.git_poll_seconds,
            idle_poll_s=cfg.idle_poll_seconds,
            grace_s=cfg.grace_hours * 3600,
        )
        self.suggestions = Suggestions(self.db)
        self.started = False
        self._last_busy = 0.0
        self.active = True  # False: another process holds the lock; this engine only manages definitions
        self._lock_fd: int | None = None
        self._promote: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[], None]] = []

    # ── network gate ─────────────────────────────────────────────────

    async def _online(self) -> None:
        # /daemon pause and restart-storm safe mode: loops, jobs and automations wait here, before a tick or an
        # occurrence is consumed, and continue after /daemon resume (a paused run_prompt would just fail or stop).
        while getattr(self.server, "halted", False) or getattr(self.server, "background_paused", False):
            await self.clock.sleep(HALT_POLL_S)
        if self._wait_online is not None:
            await self._wait_online()
        elif self._rel is not None and self._rel.netwatch is not None:
            await self._rel.netwatch.wait_until_usable()

    async def _start_netwatch(self) -> None:
        from k3code.reliability import build_reliability

        try:
            self.server._ensure_router()
            self._rel = build_reliability(self.server.config, session="automation", home=Path(self.server._home()))
            self._rel.register_providers(self.server.router.chain if self.server.router else [])
            await self._rel.start()
        except Exception:  # noqa: BLE001 - no providers configured etc.: run without the offline gate
            logger.warning("automation: netwatch unavailable, ticks will not be deferred offline", exc_info=True)
            self._rel = None

    def _user_activity(self) -> float:
        """The idle trigger's clock: the last prompt, or now while an attended session has a turn in flight (a long
        turn, or one parked on an approval or a question). Unattended runs (``background``) are not the user."""
        live = getattr(self.server, "live", {})
        if any(s.turn_in_flight and not s.background for s in list(live.values())):
            self._last_busy = self.clock.now()  # polled: idle counts from the turn's end, not from the prompt
        return max(self.server.last_user_activity, self._last_busy)

    # ── lifecycle ────────────────────────────────────────────────────

    def _take_lock(self) -> bool:
        """Non-blocking flock on ``<home>/run/automation.lock``, kept until :meth:`stop`: two gateway processes
        (the daemon and a TUI's own gateway) on one automation.db both fired every occurrence and marked each other's
        live runs interrupted. A lock that cannot be used for another reason does not block the engine."""
        if self._lock_fd is not None:
            return True
        try:
            fd = os.open(
                ensure_private_dir(Path(self.server._home()) / "run") / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600
            )
        except (OSError, RuntimeError):
            logger.warning("automation: no lock file, running without the single-instance guard", exc_info=True)
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EACCES):
                return False
            logger.warning("automation: flock failed (%s), running without the single-instance guard", e)
            return True
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd
        return True

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        if self._take_lock():
            await self._activate()
        else:
            self.active = self.loops.active = self.automations.active = False
            logger.warning(
                "another k3code process runs the loops, cron jobs and automations (%s): this one only manages "
                "definitions and takes over if that process exits",
                LOCK_NAME,
            )
            self._promote = asyncio.get_running_loop().create_task(self._promote_loop(), name="automation-promote")
        self.changed()

    async def _promote_loop(self) -> None:
        while not self._take_lock():
            await self.clock.sleep(LOCK_RETRY_S)
        logger.info("automation: the other k3code process is gone: this one runs the loops, jobs and automations now")
        self._promote = None
        await self._activate()
        self.changed()

    async def _activate(self) -> None:
        """Holder only: arm everything. Runs still marked 'running' belonged to a holder that is gone."""
        self.active = self.loops.active = self.automations.active = True
        if self._use_netwatch:
            await self._start_netwatch()
        if self._rel is not None and self._rel.netwatch is not None:
            self._rel.netwatch.subscribe(
                lambda old, new: self.automations.net_change(old.usable_for_llm, new.usable_for_llm)
            )
        stale = self.db.interrupt_stale_runs(self.clock.now())
        if stale:
            logger.info("%d run(s) were still marked running from before the restart: marked interrupted", stale)
        self.prune()
        self._maintenance = asyncio.get_running_loop().create_task(self._maintenance_loop(), name="automation-prune")
        n = self.loops.resume_all()
        self.jobs.start()
        await self.automations.start()
        logger.info("automation engine started (%d loops resumed, %d jobs)", n, self.jobs.active_count())

    #: Run history is pruned at start and then this often (wall clock).
    PRUNE_EVERY_S = 6 * 3600.0
    _maintenance: asyncio.Task[None] | None = None

    def prune(self) -> int:
        """Drop old run rows and the finished unattended sessions they pointed to; returns the sessions removed."""
        sids = self.db.prune_runs(self.clock.now())
        removed = 0
        store = getattr(self.server, "store", None)
        live = getattr(self.server, "live", {})
        for sid in dict.fromkeys(sids):
            stored = store.get(sid) if store is not None else None
            if stored is not None and sid not in live and stored.meta.get("origin") == "automation":
                removed += bool(store.delete(sid))
        if sids:
            logger.info("pruned %d old run(s), %d unattended session(s)", len(sids), removed)
        return removed

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(self.PRUNE_EVERY_S)
            try:
                self.prune()
            except Exception:  # noqa: BLE001 - housekeeping must never take the engine down
                logger.exception("automation housekeeping failed")

    async def stop(self) -> None:
        if self._promote is not None:
            self._promote.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._promote
            self._promote = None
        if self._maintenance is not None:
            self._maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance
            self._maintenance = None
        await self.loops.close()
        await self.jobs.close()
        await self.automations.stop()
        if self._rel is not None:
            with contextlib.suppress(Exception):
                await self._rel.stop()
        self.db.close()
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None
        self.started = False

    # ── status ───────────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        return {
            "loops": self.loops.active_count(),
            "jobs": self.jobs.active_count(),
            "automations": self.automations.active_count(),
        }

    def bound_to(self, session_id: str) -> bool:
        """An active loop ticks in ``session_id``, or an active automation prompts into it or watches its runs."""
        if any(r["session_id"] == session_id for r in self.db.rows("loops", "state='active'")):
            return True
        return any(
            (row["action"] or {}).get("session") == session_id or (row["trigger"] or {}).get("session") == session_id
            for row in self.db.rows("automations", "state='active'")
        )

    def references_session(self, session_id: str) -> bool:
        """Any loop that can still run, or any automation in any state (a paused one can be resumed), points at
        ``session_id``. Wider than :meth:`bound_to`: the stored-session sweep must not delete a row a resume needs."""
        if any(
            r["session_id"] == session_id for r in self.db.rows("loops", "state NOT IN ('stopped', 'done', 'failed')")
        ):
            return True
        return any(
            (row["action"] or {}).get("session") == session_id or (row["trigger"] or {}).get("session") == session_id
            for row in self.db.rows("automations")
        )

    def changed(self) -> None:
        """Push the ``⟳`` badge counts and the refreshed strip to attached clients."""
        counts = self.counts()
        counts["active"] = counts["loops"] + counts["jobs"] + counts["automations"]
        self.server.broadcast("automation.update", counts)
        self.server.broadcast_active_list()
        for cb in self._listeners:
            cb()

    def session_event(self, session_id: str, status: str, origin: str = "") -> None:
        """``session_event`` triggers: the gateway reports every finished turn (done / error / needs_input)."""
        mapped = {"done": "completed", "error": "failed", "needs_input": "needs_input"}.get(status)
        if mapped:
            self.automations.session_event(session_id, mapped, origin)
