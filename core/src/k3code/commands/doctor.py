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
        session_for = getattr(ctx, "_session_for", None)
        session = session_for(session_id) if session_for else None
        if session is not None and session.offline_protection_error:
            checks.append(
                doctor.Check(
                    "offline protection",
                    doctor.WARN,
                    f"off for this session: network watch failed to start ({session.offline_protection_error})",
                    "k3code retries it at the start of every turn; the gateway log has the traceback",
                )
            )
        text = doctor.to_json(checks) if "--json" in arg else doctor.format_report(checks)
        return {"type": "message", "message": text}
