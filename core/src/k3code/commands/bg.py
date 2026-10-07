"""/bg [prompt]: run a prompt in a background session, or hand the running foreground turn to the background."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply


class BgCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="bg", help="Run in the background: /bg <prompt>, or /bg to background the running turn")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return reply("No active session.")
        if arg:
            try:
                new = ctx.start_background(live, arg)
            except Exception as e:  # noqa: BLE001 - _InvalidParams (paused background work) etc.
                return reply(f"Cannot start a background session: {e}")
            return reply(f"Started background session {new.session_id[:8]}; it shows in the agent strip and "
                         "notifies when it finishes or needs input.", session_id=new.session_id)
        if live.streaming and live.turn_task is not None and not live.turn_task.done():
            from k3code.gateway.server import _ctx_client

            fresh = ctx.background_current(live, _ctx_client.get())
            return reply(f"Sent the running turn to the background ({live.session_id[:8]}). You are now in a "
                         f"fresh session ({fresh.session_id[:8]}).", session_id=fresh.session_id,
                         backgrounded=live.session_id)
        return reply("Nothing is running. Usage: /bg <prompt>")
