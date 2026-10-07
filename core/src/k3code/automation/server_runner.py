"""Runner backed by the gateway: unattended runs are background sessions of the daemon."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from k3code.automation.retry_policy import classify_failure
from k3code.automation.runner import RunResult, TickContext
from k3code.permissions import hardline
from k3code.providers.types import ToolSpec

if TYPE_CHECKING:
    from k3code.gateway.server import GatewayServer

_STATUS = {"done": "completed", "error": "failed", "needs_input": "needs_input", "interrupted": "interrupted"}
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

    def _cheap_model(self) -> str:
        cfg = self.server.config
        return cfg.automation.cheap_model or cfg.goal.judge_model

    async def judge(self, system: str, user: str) -> str:
        return await self.server.oneshot(system, user, model_key=self._cheap_model(), max_tokens=512)

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
    ) -> RunResult:
        srv = self.server
        if srv.background_paused:
            return RunResult(status="failed", error="background work is paused (restart-storm safe mode)", failure_kind="other")
        existing = session_id is not None
        if existing:
            live = srv.live.get(session_id or "")
            if live is None:
                stored = srv.store.get(session_id or "")
                if stored is None:
                    return RunResult(status="failed", error=f"session {session_id} no longer exists", failure_kind="other")
                live = srv.live_for(stored)
        else:
            stored = srv.store.create(title=name or "automation", model=model or srv.config.default_model,
                                      cwd=cwd or str(Path.cwd()))
            stored.meta.update({"background": True, "mode": mode, "origin": self.origin})
            srv.store.save(stored)
            live = srv.live_for(stored)
            live.background = True
        while live.streaming:  # the user (or another run) is mid-turn in this session: wait for it
            await asyncio.sleep(BUSY_POLL_S)
        prev_background = live.background
        live.background = True
        live.extra_tools = [_schedule_next_installer(tick)] if tick is not None else []
        if model and not existing:
            live.stored.model = model
        task = asyncio.get_running_loop().create_task(srv._run_turn(live, prompt), name=f"auto-{live.session_id}")
        live.turn_task = task
        srv.broadcast_active_list()
        try:
            status, text = await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as e:  # noqa: BLE001
            status, text = "error", str(e)
        finally:
            live.background = prev_background if existing else True
            live.extra_tools = []
            srv.broadcast_active_list()
        result = RunResult(
            status=_STATUS.get(status, "failed"), text=text, api_calls=live.last_api_calls, error=live.last_error,
            session_id=live.session_id,
        )
        if result.status == "failed":
            result.failure_kind, result.retry_after = classify_failure(live.last_error or text, live.last_exc)
        return result

    async def run_shell(self, command: str, cwd: str) -> tuple[int, str]:
        """Run a shell command inside the bwrap sandbox (unsandboxed with a note when bwrap is unusable)."""
        from k3code.reliability import sandbox
        from k3code.tools import tool_bash

        denied = hardline.check(command)
        if denied:
            return 126, f"denied by hardline rule: {denied}"
        workdir = cwd or str(Path.cwd())
        argv = sandbox.build_argv(workdir) if sandbox.usable() else None
        res = await tool_bash({"command": command, "timeout": SHELL_TIMEOUT_S}, cwd=Path(workdir), sandbox=argv)
        out = (res.get("stdout") or "") + (res.get("stderr") or "") + (res.get("error") or "")
        note = "" if argv else "\n[note: bwrap unavailable, ran unsandboxed]"
        return int(res.get("exit_code") if res.get("exit_code") is not None else 1), out.strip() + note

    async def start_goal(self, objective: str, session_id: str | None, cwd: str) -> RunResult:
        """Create (or reuse) a session, set a /goal on it and let the goal loop drive it."""
        srv = self.server
        if session_id is None:
            stored = srv.store.create(title=f"goal: {objective[:40]}", model=srv.config.default_model,
                                      cwd=cwd or str(Path.cwd()))
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
        return await self.run_prompt(mgr.kick_prompt() or objective, session_id=session_id)

    def notify(self, text: str, level: str = "info", key: str = "") -> None:
        payload: dict[str, Any] = {"text": text, "level": level, "kind": "automation"}
        if key:
            payload["key"] = key
        self.server.broadcast("notification.show", payload)
