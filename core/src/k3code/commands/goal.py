"""/goal <objective> [--check "<cmd>"] [--turns N] | status | pause | resume | clear."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import pop_option, reply, split_args

USAGE = '/goal <objective> [--check "<shell cmd>"] [--turns N] | status | pause | resume | clear'


class GoalCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="goal", help=USAGE)

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return reply("No active session.")
        mgr = ctx.goal_manager(live)
        args = split_args(arg)
        sub = args[0] if len(args) == 1 else ""
        if not args or sub == "status":
            return reply(mgr.status_line(), goal=mgr.snapshot())
        if sub == "pause":
            mgr.pause()
            ctx.emit_goal(live)
            return reply("Goal paused." if mgr.state else "No goal to pause.")
        if sub == "clear":
            mgr.clear()
            ctx.emit_goal(live)
            return reply("Goal cleared.")
        if sub == "resume":
            if mgr.resume() is None:
                return reply("No goal to resume.")
            ctx.emit_goal(live)
            return {
                "type": "send",
                "message": mgr.continuation_prompt() or "",
                "notice": "Goal resumed (turn budget reset).",
                "output": "Goal resumed.",
            }
        check = pop_option(args, "--check")
        turns = pop_option(args, "--turns")
        objective = " ".join(args).strip()
        if not objective:
            return reply(f"Usage: {USAGE}")
        try:
            max_turns = int(turns) if turns else None
        except ValueError:
            return reply(f"--turns needs a number, got {turns!r}")
        if max_turns is not None and max_turns < 1:
            return reply("--turns must be at least 1")
        mgr.set(objective, max_turns=max_turns, check=check)
        ctx.emit_goal(live)
        state = mgr.state
        assert state is not None
        gate = f"; check: $ {check}" if check else ""
        return {
            "type": "send",
            "message": mgr.kick_prompt() or objective,
            "display": f"/goal {objective}",
            "notice": f"⊙ Goal set ({state.max_turns} turn budget{gate}). Working…",
            "output": f"Goal set: {objective}",
        }
