"""/bg [prompt]: run a prompt in a background session, or hand the running foreground turn to the background."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, split_args
from k3code.integrations.panes import open_pane_spec


class BgCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="bg", help="Run in the background: /bg [--pane] <prompt>, or /bg to background the turn")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return reply("No active session.")
        pane = False
        if "--pane" in arg.split():
            args = split_args(arg)
            pane = pop_flag(args, "--pane")
            arg = " ".join(args)
        if arg:
            try:
                new = ctx.start_background(live, arg)
            except Exception as e:  # noqa: BLE001 - _InvalidParams (paused background work) etc.
                return reply(f"Cannot start a background session: {e}")
            extra: dict[str, Any] = {}
            where = "it shows in the agent strip and notifies when it finishes or needs input."
            if pane:
                # The process that sits in the pane (the stdio gateway or the attach bridge) opens it when it is in
                # k3 panes; anywhere else this is a plain /bg.
                extra["open_pane"] = open_pane_spec(new.session_id, name=f"bg {new.session_id[:6]}", cwd=new.stored.cwd)
                where = "opening it in a new pane (a plain background session outside k3 panes)."
            return reply(
                f"Started background session {new.session_id[:8]}; {where}", session_id=new.session_id, **extra
            )
        if live.streaming and live.turn_task is not None and not live.turn_task.done():
            from k3code.gateway.server import _ctx_client

            fresh = ctx.background_current(live, _ctx_client.get())
            return reply(
                f"Sent the running turn to the background ({live.session_id[:8]}). You are now in a "
                f"fresh session ({fresh.session_id[:8]}).",
                session_id=fresh.session_id,
                backgrounded=live.session_id,
            )
        return reply("Nothing is running. Usage: /bg <prompt>")
