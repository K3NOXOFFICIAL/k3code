"""/stats: usage per session or day from usage.db."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef
from k3code.usage import format_stats


class StatsCommand(CommandDef):
    headless = True

    def __init__(self) -> None:
        super().__init__(name="stats", help="Usage stats: /stats [session|day|turn] [days]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        parts = arg.split()
        by = parts.pop(0) if parts and parts[0] in ("session", "day", "turn") else "day"
        days: int | None = 7 if by == "day" else None
        if parts:  # what is left is a number of days; '²'.isdigit() is True and int('²') fails, hence ascii + decimal
            if len(parts) > 1 or not (parts[0].isascii() and parts[0].isdecimal()) or int(parts[0]) < 1:
                return {"type": "message", "message": "Usage: /stats [session|day|turn] [days]  (days: 1 or more)"}
            days = int(parts[0])
        rows = ctx.usage.aggregate(by, days=days)
        return {"type": "message", "message": format_stats(rows, by, getattr(ctx, "config", None))}
