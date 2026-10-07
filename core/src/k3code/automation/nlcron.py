"""Natural language → cron, via the cheap tier (the result is always validated and confirmed by the user)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from k3code.automation.cronexpr import Schedule, ScheduleError, parse_schedule

SYSTEM = (
    "Convert the user's schedule description into ONE standard 5-field cron expression "
    "(minute hour day-of-month month day-of-week; Sunday=0). Reply with only the expression, no prose, no quotes. "
    "Weekdays means 1-5. If a time is given without minutes use minute 0; 'at 9' means 09:00."
)


async def nl_to_schedule(judge: Callable[[str, str], Awaitable[str]], text: str) -> Schedule:
    reply = (await judge(SYSTEM, text)).strip().strip("`'\" \n")
    line = reply.splitlines()[0].strip().strip("`'\" ") if reply else ""
    try:
        return parse_schedule(line)
    except ScheduleError as e:
        raise ScheduleError(f"could not turn {text!r} into a schedule (model said {reply[:60]!r}): {e}") from e
