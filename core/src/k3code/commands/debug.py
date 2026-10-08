"""/debug: toggle verbose event logging; /debug dump [N] writes a redacted support bundle."""

from __future__ import annotations

from typing import Any

from k3code import debugdump
from k3code.artifacts import register_artifact
from k3code.commands import CommandDef


class DebugCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="debug", help="Toggle verbose event logging; /debug dump [N] writes a redacted bundle")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        parts = arg.split()
        if parts and parts[0] == "dump":
            n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 200
            path = await debugdump.write_bundle(ctx, n)
            register_artifact(ctx, "debug", path, title="debug bundle", session=session_id or "")
            return {"type": "message", "message": f"Debug bundle written: {path}"}
        ctx.debug = not ctx.debug
        return {"type": "message", "message": f"Verbose event logging {'ON' if ctx.debug else 'OFF'}."}
