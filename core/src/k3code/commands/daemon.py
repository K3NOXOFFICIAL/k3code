"""/daemon: show background-work state; ``/daemon resume`` leaves restart-storm safe mode."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef


class DaemonCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="daemon", help="Daemon state: /daemon [resume]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if arg == "resume":
            ctx.background_paused = False
            ctx.safe_mode_notice = ""
            ctx.emit("notification.clear", {"key": "k3.safe_mode"}, importance="essential")
            return {"type": "message", "message": "Background work resumed."}
        live = len(ctx.live)
        state = "PAUSED (restart-storm safe mode)" if ctx.background_paused else "running"
        return {
            "type": "message",
            "message": f"Background work: {state}; {live} live session(s); {len(ctx.clients)} client(s).",
        }
