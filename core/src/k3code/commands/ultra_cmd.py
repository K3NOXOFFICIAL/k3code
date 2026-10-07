"""/ultraplan, /ultracode (and /go for an ultraplan'd plan)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply


def _live(ctx: Any, session_id: str | None) -> Any:
    return ctx.sessions.get(session_id) if session_id else None


class UltraPlanCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="ultraplan", help="Deep plan: 3 independent planners + a judge: /ultraplan <task>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = _live(ctx, session_id)
        if live is None:
            return reply("No active session.")
        if not arg:
            return reply("Usage: /ultraplan <task>")

        async def job() -> str:
            up = await ctx.ultra.ultraplan(live, arg)
            live.stored.meta["ultra_plan"] = {"task": arg, "plan": up.plan, "path": str(up.path or "")}
            ctx.store.save(live.stored)
            ctx.ultra.show_plan(live, up)
            scores = ", ".join(f"{k} {v:g}" for k, v in up.scores.items())
            head = f"Plan for: {arg}\n(planners: {', '.join(up.angles)}{'; judge scores: ' + scores if scores else ''})"
            tail = (f"\n\nSaved to {up.path}. Run /go to execute it (independent steps fan out to parallel "
                    "sub-agents in worktrees).")
            return f"{head}\n\n{up.plan}{tail}" + (f"\n\nNote: {up.judge_note}" if up.judge_note else "")

        try:
            ctx.start_job(live, f"/ultraplan {arg}", job)
        except Exception as e:  # noqa: BLE001
            return reply(str(e))
        return reply("Planning from three angles (MVP-first, risk-first, architecture-first), then judging…")


class UltraCodeCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="ultracode",
            help="Plan, fan out, adversarial review, fix, test: /ultracode <task> "
            "(budget: ultracode.max_tokens / max_agents)",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = _live(ctx, session_id)
        if live is None:
            return reply("No active session.")
        if not arg:
            return reply("Usage: /ultracode <task>")
        try:
            ctx.start_job(live, f"/ultracode {arg}", lambda: ctx.ultra.ultracode(live, arg))
        except Exception as e:  # noqa: BLE001
            return reply(str(e))
        return reply("ultracode started: plan → fan-out → review panel → fixes → tests. See the agent strip.")


def go_for_ultraplan(live: Any) -> dict[str, Any] | None:
    """``/go`` after ``/ultraplan``: approve the plan for the next turn and send the task."""
    pending = live.stored.meta.get("ultra_plan") if live else None
    if not pending:
        return None
    live.preapproved_plan = dict(pending)
    live.stored.meta.pop("ultra_plan", None)
    task = str(pending["task"])
    where = f" ({Path(pending['path']).name})" if pending.get("path") else ""
    return {"type": "send", "message": task, "text": task, "notice": f"Executing the ultraplan{where}: {task}"}
