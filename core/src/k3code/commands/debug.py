"""/debug: toggle verbose event logging; /debug dump [N] writes a redacted support bundle."""

from __future__ import annotations

from typing import Any

from k3code import debugdump
from k3code.artifacts import register_artifact
from k3code.commands import CommandDef


class DebugCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="debug", help="Verbose event logging: /debug [on|off]; /debug dump [N] writes a redacted bundle"
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        parts = arg.split()
        if parts and parts[0] == "dump":
            n = 200
            if len(parts) > 1:
                if not (parts[1].isascii() and parts[1].isdecimal()) or int(parts[1]) < 1:
                    return {"type": "message", "message": "Usage: /debug dump [N]  (N: recent events, 1 or more)"}
                n = int(parts[1])
            path = await debugdump.write_bundle(ctx, n)
            register_artifact(ctx, "debug", path, title="debug bundle", session=session_id or "")
            return {"type": "message", "message": f"Debug bundle written: {path}"}
        if parts and parts[0] not in ("on", "off"):
            return {"type": "message", "message": "Usage: /debug [on|off] | dump [N]"}
        ctx.debug = (parts[0] == "on") if parts else not ctx.debug  # `/debug off` used to toggle
        return {"type": "message", "message": f"Verbose event logging {'ON' if ctx.debug else 'OFF'}."}
