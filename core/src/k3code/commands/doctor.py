"""/doctor: run the health checks and print them (``/doctor --json`` for the machine form)."""

from __future__ import annotations

from typing import Any

from k3code import doctor
from k3code.commands import CommandDef


class DoctorCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="doctor", help="Health checks with fix hints: /doctor [--json]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        checks = await doctor.run_checks(ctx.config)
        text = doctor.to_json(checks) if "--json" in arg else doctor.format_report(checks)
        return {"type": "message", "message": text}
