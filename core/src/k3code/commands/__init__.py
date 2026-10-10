"""Slash command registry: one module per command (name, aliases, help, handler)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol


class CommandContext(Protocol):
    """What a command handler may use (subset of GatewayServer, duck-typed for tests)."""

    config: Any
    store: Any


@dataclass
class CommandDef:
    name: str
    help: str
    aliases: list[str] = field(default_factory=list)
    #: Works in a one-shot ``k3code -p "/name"`` run: no live session, client or model needed. The others refuse there.
    headless: ClassVar[bool] = False

    async def handle(self, ctx: CommandContext, session_id: str | None, arg: str) -> dict[str, Any]:
        raise NotImplementedError


class CommandRegistry:
    """Maps command name/alias → CommandDef. dispatch() returns a CommandDispatchResult-shaped dict."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandDef] = {}

    def register(self, cmd: CommandDef) -> None:
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def names(self) -> list[str]:
        seen: list[str] = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.append(cmd.name)
        return sorted(seen)

    def get(self, name: str) -> CommandDef | None:
        return self._commands.get(name.lstrip("/"))

    async def dispatch(self, ctx: CommandContext, name: str, arg: str, session_id: str | None) -> dict[str, Any]:
        cmd = self.get(name)
        if cmd is None:
            available = ", ".join(f"/{n}" for n in self.names())
            msg = f"Unknown command: /{name}. Available: {available}"
            return {"type": "message", "message": msg, "output": msg}
        try:
            result = await cmd.handle(ctx, session_id, arg.strip())
        except Exception as e:  # noqa: BLE001 - surface as chat text, never crash the gateway
            msg = f"/{cmd.name} failed: {e}"
            # `error` lets `k3code -p` exit non-zero; chat clients render `output` and ignore it
            return {"type": "message", "message": msg, "output": msg, "error": "command_failed"}
        if not isinstance(result, dict) or "type" not in result:
            return {"type": "message", "message": str(result), "output": str(result)}
        if result["type"] == "message":
            result.setdefault("output", result.get("message", ""))  # slash.exec clients render `output`
        return result
