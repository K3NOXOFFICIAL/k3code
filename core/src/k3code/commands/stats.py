"""/stats: usage per session or day from usage.db."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef
from k3code.usage import format_stats


class StatsCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="stats", help="Usage stats: /stats [session|day] [days]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        parts = arg.split()
        by = parts[0] if parts and parts[0] in ("session", "day") else "day"
        days = next((int(p) for p in parts if p.isdigit()), 7 if by == "day" else None)
        rows = ctx.usage.aggregate(by, days=days)
        return {"type": "message", "message": format_stats(rows, by)}
