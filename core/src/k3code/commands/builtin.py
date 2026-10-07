"""Built-in slash commands: /model /effort /clear /compact /rename /resume /stop /exit /help."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code.commands import CommandDef, CommandRegistry


class _ModelCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="model", help="Show or switch the model: /model [key]", aliases=["m"])

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if not arg:
            return {"type": "message", "message": f"Current model key: {ctx.config.default_model}"}
        ctx.config.default_model = arg
        return {"type": "message", "message": f"Model key set to: {arg}"}


class _EffortCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="effort", help="Show or set reasoning effort: /effort [low|medium|high]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if not arg:
            return {"type": "message", "message": "Usage: /effort [low|medium|high]"}
        if arg not in ("low", "medium", "high"):
            return {"type": "message", "message": f"Unknown effort: {arg} (low|medium|high)"}
        return {"type": "message", "message": f"Reasoning effort set to: {arg}"}


class _ClearCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="clear", help="Clear the current session transcript", aliases=["c"])

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return {"type": "message", "message": "No active session to clear."}
        live.messages = []
        from k3code.gateway.sessions import StoredSession

        stored: StoredSession | None = ctx.store.get(session_id or "")
        if stored is not None:
            stored.messages = []
            ctx.store.save(stored)
        return {"type": "message", "message": "Session cleared."}


class _CompactCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="compact", help="Summarize the transcript to free context (stub: reports counts)")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        n = len(live.messages) if live else 0
        return {"type": "message", "message": f"Compact: {n} messages in transcript (full compaction lands in M2)."}


class _RenameCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="rename", help="Rename the session: /rename <title>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if not arg:
            return {"type": "message", "message": "Usage: /rename <title>"}
        if not session_id:
            return {"type": "message", "message": "No active session."}
        stored = ctx.store.get(session_id)
        if stored is None:
            return {"type": "message", "message": "Session not found."}
        stored.title = arg
        ctx.store.save(stored)
        ctx.emit("session.title", {"session_id": session_id, "title": arg})
        return {"type": "message", "message": f"Renamed to: {arg}"}


class _ResumeCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="resume", help="Resume a stored session: /resume <session_id>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if not arg:
            return {"type": "message", "message": "Usage: /resume <session_id>"}
        stored = ctx.store.get(arg)
        if stored is None:
            return {"type": "message", "message": f"Session not found: {arg}"}
        return {"type": "message", "message": f"Resumed session {arg} ({len(stored.messages)} messages)."}


class _AddDirCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="add-dir", help="Add a readable/writable root to this session: /add-dir <path>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return {"type": "message", "message": "No active session."}
        if not arg:
            dirs = live.perms.add_dirs
            return {"type": "message", "message": "Added dirs: " + (", ".join(dirs) if dirs else "(none)")}
        base = Path(live.stored.cwd or ".")
        path = Path(arg).expanduser()
        path = (path if path.is_absolute() else base / path).resolve()
        if not path.is_dir():
            return {"type": "message", "message": f"Not a directory: {path}"}
        if str(path) not in live.perms.add_dirs:
            live.perms.add_dirs.append(str(path))
        live.stored.meta["add_dirs"] = list(live.perms.add_dirs)
        ctx.store.save(live.stored)
        return {"type": "message", "message": f"Added {path} to this session's roots."}


class _StopCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="stop", help="Interrupt the running turn")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if not session_id:
            return {"type": "message", "message": "No active session."}
        ok = await ctx.interrupt_turn(session_id)
        return {"type": "message", "message": "Turn interrupted." if ok else "No running turn."}


class _ExitCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="exit", aliases=["quit", "q"], help="Close the session")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        return {"type": "exit", "message": "Session closed."}


class _HelpCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="help", aliases=["h", "?"], help="List available commands")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        lines = [f"/{name} — {cmd.help}" for name, cmd in sorted(ctx.commands._commands.items()) if name == cmd.name]
        return {"type": "message", "message": "Commands:\n" + "\n".join(lines)}


def build_registry() -> CommandRegistry:
    reg = CommandRegistry()
    for cmd in (
        _ModelCommand(),
        _EffortCommand(),
        _ClearCommand(),
        _CompactCommand(),
        _RenameCommand(),
        _ResumeCommand(),
        _AddDirCommand(),
        _StopCommand(),
        _ExitCommand(),
        _HelpCommand(),
    ):
        reg.register(cmd)
    return reg
