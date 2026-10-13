"""/import <path> [--yes] [--trust-bundle] [--settings-only|--session-only]: merge a .k3bundle into this install."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code.bundle import BundleError, apply_bundle, pending_sensitive, read_bundle
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, session_cwd, split_args

USAGE = "/import <path> [--yes] [--trust-bundle] [--settings-only|--session-only]"


class ImportCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="import", help=f"Import a .k3bundle: {USAGE}")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        # both flags are popped: with `-y --yes x` the short circuit left `-y` as the path
        long_yes, short_yes = pop_flag(args, "--yes"), pop_flag(args, "-y")
        yes = long_yes or short_yes
        settings_only = pop_flag(args, "--settings-only")
        session_only = pop_flag(args, "--session-only")
        trust_bundle = pop_flag(args, "--trust-bundle")
        if not args or (settings_only and session_only):
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
        # (a yes is kept for the item it was given for: its key and the text shown, not the key alone, since the same
        # key can come from the user settings and from the project's when those are the same file)
        accepted: set[tuple[str, str]] = set()
        if not trust_bundle and not yes and not session_only:
            for item in pending_sensitive(bundle, cwd):
                answer = await ctx.clarify(item.text() + f"\nApply {item.key}?", ["Skip", "Apply"], session_id)
                if str(answer.get("answer", "")).strip().lower() in ("apply", "yes", "y"):
                    accepted.add((item.key, item.text()))
        try:
            rep = apply_bundle(
                bundle,
                store=ctx.store,
                cwd=cwd,
                settings=not session_only,
                sessions=not settings_only,
                accept=lambda item: (item.key, item.text()) in accepted,
                trust_bundle=trust_bundle,
            )
        except (BundleError, ValueError) as e:
            return reply(f"Import failed: {e}")
        ctx.apply_file_config(cwd)
        return reply(f"Imported.\n{rep.describe()}", report={"sessions": rep.sessions, "backups": rep.backups})
