"""/daemon: show background-work state; ``/daemon pause`` halts everything, ``/daemon resume`` clears it."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef


class DaemonCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="daemon", help="Daemon state: /daemon [pause|resume]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if arg == "pause":
            stopped = await ctx.halt_daemon("/daemon pause")
            return {
                "type": "message",
                "message": f"Daemon halted: {stopped} running turn(s) stopped; loops, jobs and sub-agents wait. "
                "/daemon resume to continue.",
            }
        if arg == "resume":
            ctx.resume_daemon()
            return {"type": "message", "message": "Background work resumed."}
        live = len(ctx.live)
        if ctx.halted:
            state = f"HALTED ({ctx.halt.reason})"
        elif ctx.background_paused:
            state = "PAUSED (restart-storm safe mode)"
        else:
            state = "running"
        return {
            "type": "message",
            "message": f"Background work: {state}; {live} live session(s); {len(ctx.clients)} client(s).",
        }
