"""/import <path> [--yes] [--trust-bundle] [--settings-only|--session-only]: merge a .k3bundle into this install."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code.bundle import BundleError, apply_bundle, read_bundle, sensitive_items
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, session_cwd, split_args

USAGE = "/import <path> [--yes] [--trust-bundle] [--settings-only|--session-only]"


class ImportCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="import", help=f"Import a .k3bundle: {USAGE}")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        yes = pop_flag(args, "--yes") or pop_flag(args, "-y")
        settings_only = pop_flag(args, "--settings-only")
        session_only = pop_flag(args, "--session-only")
        trust_bundle = pop_flag(args, "--trust-bundle")
        if not args:
            return reply(f"Usage: {USAGE}")
        cwd = session_cwd(ctx, session_id)
        path = Path(args[0]).expanduser()
        if not path.is_absolute():
            path = cwd / path
        try:
            bundle = read_bundle(path)
        except BundleError as e:
            return reply(f"Import failed: {e}")
        summary = bundle.describe()
        if not yes:
            answer = await ctx.clarify(
                f"Import this bundle? Existing config is backed up first.\n{summary}",
                ["Import", "Cancel"],
                session_id,
            )
            if str(answer.get("answer", "")).strip().lower() not in ("import", "yes", "y"):
                return reply("Import cancelled.")
        # MCP servers, permission rules, provider endpoints and hooks: one explicit yes each. --yes does not cover
        # them (only --trust-bundle does); with --yes and no --trust-bundle they are skipped with a warning.
        accepted: set[str] = set()
        if not trust_bundle and not yes and not session_only:
            for item in sensitive_items(bundle):
                answer = await ctx.clarify(item.text() + f"\nApply {item.key}?", ["Skip", "Apply"], session_id)
                if str(answer.get("answer", "")).strip().lower() in ("apply", "yes", "y"):
                    accepted.add(item.key)
        try:
            rep = apply_bundle(
                bundle,
                store=ctx.store,
                cwd=cwd,
                settings=not session_only,
                sessions=not settings_only,
                accept=lambda item: item.key in accepted,
                trust_bundle=trust_bundle,
            )
        except (BundleError, ValueError) as e:
            return reply(f"Import failed: {e}")
        ctx.apply_file_config(cwd)
        return reply(f"Imported.\n{rep.describe()}", report={"sessions": rep.sessions, "backups": rep.backups})
