"""The automation engine: owns loops, the job scheduler, automations and suggestions inside the daemon."""

from __future__ import annotations

import contextlib
import logging
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
from k3code.reliability.governor import Governor, GovernorConfig

if TYPE_CHECKING:
    from k3code.gateway.server import GatewayServer

logger = logging.getLogger("k3code.automation")


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
            activity=lambda: server.last_user_activity,
            on_change=self.changed,
            webhook_port=cfg.webhook_port,
            git_poll_s=cfg.git_poll_seconds,
            idle_poll_s=cfg.idle_poll_seconds,
            grace_s=cfg.grace_hours * 3600,
        )
        self.suggestions = Suggestions(self.db)
        self.started = False
        self._listeners: list[Callable[[], None]] = []

    # ── network gate ─────────────────────────────────────────────────

    async def _online(self) -> None:
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

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        if self._use_netwatch:
            await self._start_netwatch()
        if self._rel is not None and self._rel.netwatch is not None:
            self._rel.netwatch.subscribe(
                lambda old, new: self.automations.net_change(old.usable_for_llm, new.usable_for_llm)
            )
        n = self.loops.resume_all()
        self.jobs.start()
        await self.automations.start()
        logger.info("automation engine started (%d loops resumed, %d jobs)", n, self.jobs.active_count())
        self.changed()

    async def stop(self) -> None:
        await self.loops.close()
        await self.jobs.close()
        await self.automations.stop()
        if self._rel is not None:
            with contextlib.suppress(Exception):
                await self._rel.stop()
        self.db.close()
        self.started = False

    # ── status ───────────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        return {
            "loops": self.loops.active_count(),
            "jobs": self.jobs.active_count(),
            "automations": self.automations.active_count(),
        }

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
