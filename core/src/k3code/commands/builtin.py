"""Built-in slash commands: /model /effort /clear /compact /rename /resume /stop /exit /help."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code import chain_config
from k3code.commands import CommandDef, CommandRegistry
from k3code.commands.autonomy import AdvisorCommand, GoCommand, PreviewCommand, ProposalsCommand, ScopeCommand
from k3code.commands.daemon import DaemonCommand
from k3code.commands.debug import DebugCommand
from k3code.commands.doctor import DoctorCommand
from k3code.commands.stats import StatsCommand
from k3code.commands.update_cmd import UpdateCommand
from k3code.config import load_config
from k3code.providers.effort import LEVELS as EFFORT_LEVELS
from k3code.session_ai import compact_messages


class _ModelCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="model", help="Show or switch the model: /model [key]", aliases=["m"])

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if arg.split()[:1] == ["chain"]:
            return self._chain(ctx, session_id, arg.split()[1:])
        live = ctx.sessions.get(session_id) if session_id else None
        if not arg:
            current = (live.stored.model if live is not None else "") or ctx.config.default_model
            return {"type": "message", "message": f"Current model key: {current}"}
        key, _, reason = arg.partition(" ")
        # like config.set model: a known key, set on the session (the next turn routes on stored.model); it used to
        # change only config.default_model, which a session with its own model never reads, and took any typo
        known = {m for p in ctx.config.providers for m in p.models} | {ctx.config.default_model}
        if key not in known:
            return {"type": "message", "message": f"Unknown model key: {key} (known: {', '.join(sorted(known))})"}
        old = (live.stored.model if live is not None else "") or ctx.config.default_model
        if hasattr(ctx, "learning") and key != old:
            kind = getattr(live, "current_kind", "") if live is not None else ""
            ctx.learning.record("model_switch", live, subject=f"{old} -> {key}", choice=key,
                                detail={"from": old, "to": key, "reason": reason.strip(), "task_kind": kind or ""})
        if live is None:
            ctx.config.default_model = key
        else:
            live.stored.model = key
            ctx.store.save(live.stored)
            live.emit("session.info", live.live_info())
        return {"type": "message", "message": f"Model key set to: {key}"}


    def _chain(self, ctx: Any, session_id: str | None, args: list[str]) -> dict[str, Any]:
        """/model chain [add|remove|move …]: show the fallback chain, or edit the user config."""
        if not args:
            return {"type": "message", "message": chain_config.format_chain(chain_config.chain_rows(ctx, session_id))}
        try:
            backup = chain_config.edit_config(args[0], args[1:])
        except chain_config.ChainEditError as e:
            return {"type": "message", "message": f"/model chain: {e}"}
        reloaded = load_config(project_dir=Path.cwd())
        ctx.config.providers = reloaded.providers
        if hasattr(ctx, "_chain_key"):
            ctx._chain_key = None  # rebuild the router on the next turn
        note = f" (backup: {backup})" if backup else ""
        return {
            "type": "message",
            "message": f"Chain updated{note}.\n" + chain_config.format_chain(chain_config.chain_rows(ctx, session_id)),
        }


class _EffortCommand(CommandDef):
    _USAGE = "/effort [low|medium|high|xhigh|max|default]"

    def __init__(self) -> None:
        super().__init__(name="effort", help=f"Show or set reasoning effort: {self._USAGE}")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        arg = arg.strip().lower()
        if not arg:
            current = getattr(live, "reasoning_effort", None) or "default"
            return {"type": "message", "message": f"Reasoning effort: {current}. Usage: {self._USAGE}"}
        if arg not in (*EFFORT_LEVELS, "default"):
            return {"type": "message", "message": f"Unknown effort: {arg}. Usage: {self._USAGE}"}
        if live is None:
            return {"type": "message", "message": "No active session."}
        # Sent from the next turn on: as output_config.effort to Claude models that take it, and as
        # reasoning_effort to OpenAI reasoning models. Other models ignore it.
        value = None if arg == "default" else arg
        live.reasoning_effort = value
        if value is None:
            live.stored.meta.pop("reasoning_effort", None)
        else:
            live.stored.meta["reasoning_effort"] = value
        ctx.store.save(live.stored)
        live.emit("session.info", live.live_info())
        return {"type": "message", "message": f"Reasoning effort set to: {arg}"}


class _ClearCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="clear", help="Clear the current session transcript", aliases=["c"])

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return {"type": "message", "message": "No active session to clear."}
        if getattr(live, "streaming", False):  # the running turn's persist would put the transcript right back
            return {"type": "message", "message": "A turn is running in this session; /stop it first."}
        live.messages = []
        from k3code.gateway.sessions import StoredSession

        stored: StoredSession | None = ctx.store.get(session_id or "")
        if stored is not None:
            stored.messages = []
            ctx.store.save(stored)
        return {"type": "message", "message": "Session cleared."}


class _CompactCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="compact", help="Summarize the older transcript to free context")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return {"type": "message", "message": "No active session."}
        if getattr(live, "streaming", False):  # the running turn's persist would overwrite the summary
            return {"type": "message", "message": "A turn is running in this session; /compact when it ends."}
        before = len(live.messages)
        try:
            messages, folded = await compact_messages(
                ctx.model_caller, list(live.messages), session_id=live.session_id
            )
        except Exception as e:  # noqa: BLE001 - e.g. every provider rate-limited
            return {"type": "message", "message": f"Compact failed: {e}"}
        if not folded:
            return {"type": "message", "message": f"Nothing to compact ({before} messages)."}
        live.messages = messages
        ctx.store.save(live.stored)
        note = f"Compacted {folded} messages into a summary ({before} → {len(messages)})."
        return {"type": "message", "message": note}


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
        lines = [
            f"/{name} — {cmd.help.splitlines()[0] if cmd.help else ''}"
            for name, cmd in sorted(ctx.commands._commands.items())
            if name == cmd.name
        ]
        return {"type": "message", "message": "Commands:\n" + "\n".join(lines)}


def build_registry() -> CommandRegistry:
    from k3code.commands.artifacts_cmd import ArtifactsCommand
    from k3code.commands.automations_cmd import AutomationsCommand
    from k3code.commands.bg import BgCommand
    from k3code.commands.branch import BranchCommand
    from k3code.commands.config_cmd import ConfigCommand
    from k3code.commands.export import ExportCommand
    from k3code.commands.fork import ForkCommand
    from k3code.commands.goal import GoalCommand
    from k3code.commands.import_ import ImportCommand
    from k3code.commands.learning_cmd import (
        LearnCommand,
        OptimizerCommand,
        PermissionsCommand,
        SelfImproveCommand,
        UpdateConfigCommand,
    )
    from k3code.commands.loop import LoopCommand
    from k3code.commands.mcp_cmd import McpCommand
    from k3code.commands.memory_cmd import MemoryCommand
    from k3code.commands.output_style import OutputStyleCommand
    from k3code.commands.research_cmd import UltraResearchCommand
    from k3code.commands.review import ReviewCommand
    from k3code.commands.schedule import ScheduleCommand
    from k3code.commands.settings_cmd import FocusCommand, SettingsCommand
    from k3code.commands.skills_cmd import SkillsCommand
    from k3code.commands.ultra_cmd import UltraCodeCommand, UltraPlanCommand

    reg = CommandRegistry()
    for extra in (
        ExportCommand(), ImportCommand(), ForkCommand(), BranchCommand(), SettingsCommand(), ConfigCommand(),
        OutputStyleCommand(), MemoryCommand(), SkillsCommand(), McpCommand(), ReviewCommand(), GoalCommand(),
        LoopCommand(), ScheduleCommand(), AutomationsCommand(),
        ArtifactsCommand(), BgCommand(), UltraPlanCommand(), UltraCodeCommand(),
        UltraResearchCommand(),
        PermissionsCommand(), FocusCommand(), UpdateConfigCommand(), OptimizerCommand(), SelfImproveCommand(),
        LearnCommand(),
    ):
        reg.register(extra)
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
        DoctorCommand(),
        UpdateCommand(),
        StatsCommand(),
        DebugCommand(),
        DaemonCommand(),
        ScopeCommand(),
        ProposalsCommand(),
        PreviewCommand(),
        GoCommand(),
        AdvisorCommand(),
    ):
        reg.register(cmd)
    return reg
