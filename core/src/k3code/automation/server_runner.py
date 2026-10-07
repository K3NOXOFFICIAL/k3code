"""Runner backed by the gateway: unattended runs are background sessions of the daemon."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from k3code.automation.retry_policy import classify_failure
from k3code.automation.runner import RunResult, TickContext
from k3code.permissions import hardline
from k3code.providers.types import ToolSpec
from k3code.routing.tiers import TaskKind

if TYPE_CHECKING:
    from k3code.gateway.server import GatewayServer

_STATUS = {"done": "completed", "error": "failed", "needs_input": "needs_input", "interrupted": "interrupted"}
KEEP_FINISHED_RUNS = 20  # finished unattended sessions kept live (visible in the strip)
BUSY_POLL_S = 1.0
SHELL_TIMEOUT_S = 600.0


def _schedule_next_installer(tick: TickContext) -> Any:
    async def handler(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
        try:
            seconds = float(arguments.get("seconds"))
        except (TypeError, ValueError):
            return {"error": "schedule_next needs a numeric `seconds`"}
        applied = tick.schedule_next(seconds, str(arguments.get("reason") or ""))
        note = "" if applied == seconds else f" (clamped from {seconds:g}s to the 60-3600s range)"
        return {"content": f"Next tick in {applied:g}s{note}."}

    spec = ToolSpec(
        name="schedule_next",
        description="Self-paced /loop only: say how many seconds from now the next tick should run (60-3600) and why.",
        parameters={
            "type": "object",
            "properties": {"seconds": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["seconds"],
        },
        side_effect=False,
    )

    def install(reg: Any) -> None:
        reg.register(spec, handler)

    return install


class ServerRunner:
    def __init__(self, server: GatewayServer, origin: str = "automation") -> None:
        self.server = server
        self.origin = origin

    async def judge(self, system: str, user: str, kind: str = "goal_judge") -> str:
        """Tool-less side call through ModelCaller (tier policy, usage rows): ``goal_judge`` / ``classification``."""
        return await self.server.oneshot(system, user, kind=TaskKind(kind), max_tokens=512)

    async def run_prompt(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str = "",
        model: str = "",
        name: str = "",
        mode: str = "auto",
        tick: TickContext | None = None,
        kind: str = "background_turn",
    ) -> RunResult:
        srv = self.server
        if srv.background_paused:
            return RunResult(
                status="failed", error="background work is paused (restart-storm safe mode)", failure_kind="other"
            )
        existing = session_id is not None
        if existing:
            live = srv.live.get(session_id or "")
            if live is None:
                stored = srv.store.get(session_id or "")
                if stored is None:
                    return RunResult(
                        status="failed", error=f"session {session_id} no longer exists", failure_kind="other"
                    )
                live = srv.live_for(stored)
        else:
            stored = srv.store.create(
                title=name or "automation", model=model or srv.config.default_model, cwd=cwd or str(Path.cwd())
            )
            stored.meta.update({"background": True, "mode": mode, "origin": self.origin})
            srv.store.save(stored)
            live = srv.live_for(stored)
            live.background = True
        # One unattended run per session at a time: the busy check below and the point where the turn marks itself
        # streaming are separated by awaits, so two callers (two /loop commands on one session, an overdue loop
        # resumed twice) both passed it and ran concurrently, clobbering each other's background/tool flags.
        async with live.run_lock:
            while live.streaming:  # the user (or another run) is mid-turn in this session: wait for it
                await asyncio.sleep(BUSY_POLL_S)
            prev_background = live.background
            live.background = True
            live.extra_tools = [_schedule_next_installer(tick)] if tick is not None else []
            live.task_kind = kind  # tier policy: loop_tick / cron_job / background_turn (cheap by default)
            if model and not existing:
                live.stored.model = model
            task = asyncio.get_running_loop().create_task(srv._run_turn(live, prompt), name=f"auto-{live.session_id}")
            live.turn_task = task
            srv.broadcast_active_list()
            try:
                status, text = await task
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if task.cancelled() and not (me is not None and me.cancelling()):
                    # /stop (or deleting the session) cancelled the *turn*; this caller is fine. Re-raising took the
                    # cron job / loop / automation supervisor down with it (next_run_at never advanced, so a cron job
                    # re-fired at once; a loop task died while its row stayed 'active').
                    status, text = "interrupted", ""
                else:
                    task.cancel()
                    raise
            except Exception as e:  # noqa: BLE001
                status, text = "error", str(e)
            finally:
                live.background = prev_background if existing else True
                live.extra_tools = []
                live.task_kind = ""
                if not existing:
                    await self._release(live)
                srv.broadcast_active_list()
        result = RunResult(
            status=_STATUS.get(status, "failed"),
            text=text,
            api_calls=live.last_api_calls,
            error=live.last_error,
            session_id=live.session_id,
        )
        if result.status == "failed":
            result.failure_kind, result.retry_after = classify_failure(live.last_error or text, live.last_exc)
        return result

    async def _release(self, live: Any) -> None:
        """A finished unattended session must not keep a netwatch loop alive, and ``server.live`` must not grow
        without bound with a ``* * * * *`` job: stop its reliability bundle and keep only the newest finished runs."""
        if live.reliability is not None:
            with contextlib.suppress(Exception):
                await live.reliability.stop()  # re-arms on the next turn
        finished = [
            s
            for s in self.server.live.values()
            if s.stored.meta.get("origin") == self.origin and not s.streaming and s.pending_approval is None
        ]
        finished.sort(key=lambda s: s.stored.updated_at)
        for old in finished[: max(0, len(finished) - KEEP_FINISHED_RUNS)]:
            self.server.live.pop(old.session_id, None)  # stays in the session store, resumable by id
            if old.reliability is not None:  # an evicted session's netwatch tasks would otherwise run forever
                with contextlib.suppress(Exception):
                    await old.reliability.stop()

    async def run_shell(self, command: str, cwd: str) -> tuple[int, str]:
        """Run a shell command inside the bwrap sandbox (unsandboxed with a note when bwrap is unusable)."""
        from k3code.reliability import sandbox
        from k3code.tools import tool_bash

        denied = hardline.check(command)
        if denied:
            return 126, f"denied by hardline rule: {denied}"
        workdir = cwd or str(Path.cwd())
        # usable() spawns bwrap (up to 10 s on first use): keep it off the event loop
        argv = sandbox.build_argv(workdir) if await asyncio.to_thread(sandbox.usable) else None
        res = await tool_bash({"command": command, "timeout": SHELL_TIMEOUT_S}, cwd=Path(workdir), sandbox=argv)
        out = (res.get("stdout") or "") + (res.get("stderr") or "") + (res.get("error") or "")
        note = "" if argv else "\n[note: bwrap unavailable, ran unsandboxed]"
        return int(res.get("exit_code") if res.get("exit_code") is not None else 1), out.strip() + note

    async def start_goal(self, objective: str, session_id: str | None, cwd: str) -> RunResult:
        """Create (or reuse) a session, set a /goal on it and let the goal loop drive it."""
        srv = self.server
        created = session_id is None
        if session_id is None:
            stored = srv.store.create(
                title=f"goal: {objective[:40]}", model=srv.config.default_model, cwd=cwd or str(Path.cwd())
            )
            stored.meta.update({"background": True, "mode": "auto", "origin": self.origin})
            srv.store.save(stored)
            live = srv.live_for(stored)
            live.background = True
            session_id = stored.session_id
        else:
            live = srv.live.get(session_id) or srv.live_for(srv.store.get(session_id))
        mgr = srv.goal_manager(live)
        mgr.set(objective, max_turns=None, check=None)
        srv.emit_goal(live)
        try:
            return await self.run_prompt(mgr.kick_prompt() or objective, session_id=session_id)
        finally:
            if created:  # run_prompt sees an existing session here and skips its own release: do it for the fresh one
                await self._release(live)

    def notify(self, text: str, level: str = "info", key: str = "") -> None:
        payload: dict[str, Any] = {"text": text, "level": level, "kind": "ttl", "ttl_ms": 12000}
        if key:
            payload["key"] = key
        self.server.broadcast("notification.show", payload)
