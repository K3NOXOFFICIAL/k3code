"""M5 commands: /permissions suggest, /update-config, /optimizer, /self-improve, /learn."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply, session_cwd
from k3code.learning import optimizer, permrules, updateconfig
from k3code.paths import user_config_path


class PermissionsCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="permissions", help="/permissions suggest — rule candidates mined from your approvals")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if arg.split()[:1] != ["suggest"]:
            return reply("Usage: /permissions suggest")
        hub = ctx.learning
        c = hub.cfg
        cwd = str(session_cwd(ctx, session_id))
        cands = permrules.mine(hub.log, min_approvals=int(c["perm_min_approvals"]),
                               min_denials=int(c["perm_min_denials"]),
                               user_projects=int(c["user_scope_projects"]), cwd=cwd)
        live = ctx.sessions.get(session_id) if session_id else None
        made = permrules.to_proposals(cands, hub.store, session_id or "")
        if live is not None:
            hub.emit(live, made)
        return reply(permrules.format_candidates(cands))


class UpdateConfigCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="update-config", help="Change settings in plain words: /update-config <request>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        return reply(await updateconfig.run(ctx, session_id, arg, user_config_path()))


class OptimizerCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="optimizer", help="/optimizer status | run | rollback <id>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        hub = ctx.learning
        parts = arg.split()
        sub = parts[0] if parts else "status"
        if sub == "status":
            on = "on" if hub.cfg["optimizer"]["enabled"] else "off (opt in: learning.optimizer.enabled: true)"
            return reply(f"Optimizer: {on}\n{hub.experiments.format_status()}")
        if sub == "run":
            live = ctx.sessions.get(session_id) if session_id else None
            m = hub.metrics(since=hub.clock() - 7 * 86400)
            made = optimizer.propose(m, ctx.config, hub.store)
            if live is not None:
                hub.emit(live, made)
            return reply(f"Analysed {m['sessions']} sessions (score {optimizer.score(m)}); "
                         f"{len(made)} new overlay proposal(s).")
        if sub == "rollback":
            if len(parts) < 2:
                return reply("Usage: /optimizer rollback <id>")
            return reply("Rolled back." if hub.experiments.rollback(parts[1]) else f"No active experiment {parts[1]}.")
        return reply("Usage: /optimizer status|run|rollback <id>")


class SelfImproveCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="self-improve", help="Draft an issue about improving k3code: /self-improve <title>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        if not arg:
            return reply("Usage: /self-improve <what should be improved>")
        path = optimizer.write_issue_draft(session_cwd(ctx, session_id), arg[:80], arg)
        return reply(f"Issue draft written to {path} (opening a PR is not automated yet).")


class LearnCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="learn", help="/learn run — distill preferences and curate skills now | prefs")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        hub = ctx.learning
        if arg.strip() == "prefs":
            prefs = hub.preferences(20)
            return reply("\n".join(f"- {p}" for p in prefs) or "Nothing learned yet.")
        live = ctx.sessions.get(session_id) if session_id else None
        done = await hub.maintenance(live, force=True)
        return reply(f"Learning pass done: {done}")


__all__ = ["PermissionsCommand", "UpdateConfigCommand", "OptimizerCommand", "SelfImproveCommand", "LearnCommand"]
