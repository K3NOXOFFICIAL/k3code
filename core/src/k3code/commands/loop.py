"""/loop [interval] <prompt> [--times N] [--until "<cond>"] [--max-ticks N] | status | stop [id] | list."""

from __future__ import annotations

from typing import Any

from k3code.automation.cronexpr import ScheduleError, parse_schedule
from k3code.automation.loops import DEFAULT_MAX_TICKS
from k3code.commands import CommandDef
from k3code.commands._util import pop_option, reply, split_args

USAGE = (
    '/loop [interval] <prompt> [--times N] [--until "<condition>"] [--max-ticks N]  |  /loop status | stop [id] | list\n'
    "interval: 5m, 1h, daily 09:00 — omit it for a self-paced loop (the model picks the next delay)."
)


def split_interval(args: list[str]) -> tuple[str | None, list[str]]:
    """Peel a leading interval (``5m`` / ``daily 09:00`` / ``every 2h``) off the arguments."""
    if len(args) >= 2 and args[0].lower() in ("daily", "every"):
        try:
            parse_schedule(" ".join(args[:2]))
            return " ".join(args[:2]), args[2:]
        except ScheduleError:
            pass
    if args:
        try:
            parse_schedule(args[0])
            return args[0], args[1:]
        except ScheduleError:
            pass
    return None, args


class LoopCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="loop", help=USAGE)

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        eng = await ctx.ensure_automation()
        args = split_args(arg)
        sub = args[0] if args else "status"
        if sub in ("status", "list") and len(args) <= 1:
            rows = eng.loops.list(active_only=(sub == "status")) if sub == "list" else (
                [r for r in eng.loops.list(active_only=True) if not session_id or r["session_id"] == session_id]
            )
            if not rows:
                return reply("No loops." if sub == "list" else "No active loops in this session.")
            return reply("\n".join(eng.loops.describe(r) for r in rows), loops=[r["id"] for r in rows])
        if sub == "stop":
            ref = args[1] if len(args) > 1 else None
            stopped = eng.loops.stop(ref, session_id=session_id)
            return reply(f"Stopped loop(s): {', '.join(stopped)}" if stopped else "No matching active loop.")
        if session_id is None or ctx.sessions.get(session_id) is None:
            return reply("No active session.")
        times = pop_option(args, "--times")
        until = pop_option(args, "--until")
        max_ticks = pop_option(args, "--max-ticks")
        try:
            n_times = int(times) if times else None
            n_max = int(max_ticks) if max_ticks else DEFAULT_MAX_TICKS
        except ValueError:
            return reply("--times and --max-ticks need whole numbers.")
        interval, rest = split_interval(args)
        prompt = " ".join(rest).strip()
        if not prompt:
            return reply(f"Usage: {USAGE}")
        live = ctx.sessions.get(session_id)
        row = eng.loops.create(
            session_id=session_id, prompt=prompt, interval=interval, times=n_times, until=until, max_ticks=n_max,
            cwd=live.stored.cwd or "",
        )
        mode = f"every {interval}" if interval else "self-paced"
        return reply(f"⟳ Loop {row['id']} started ({mode}, max {n_max} ticks). Stop it with /loop stop {row['id']}.",
                     loop=row["id"])
