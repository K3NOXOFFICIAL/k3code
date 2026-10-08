"""/update: show current vs latest version + changelog; ``/update now`` applies, ``/update rollback`` reverts."""

from __future__ import annotations

import asyncio
from typing import Any

from k3code import update as upd
from k3code.commands import CommandDef
from k3code.commands._util import reply


class UpdateCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="update", help="Check for updates: /update [now|rollback]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        sub = arg.strip().lower()
        if sub == "rollback":
            res = await asyncio.to_thread(upd.rollback)
            return reply(res.message)
        cfg = upd.update_settings()
        cur = upd.current_version() or "(not a versioned install)"
        try:
            token = upd.github_token()
            rel = await asyncio.to_thread(upd.fetch_latest, cfg["channel"], cfg["repo"], token)
        except Exception as e:  # noqa: BLE001
            return reply(f"current: {cur}\nCould not check releases: {e}")
        if rel is None:
            return reply(f"current: {cur}\nNo releases on channel '{cfg['channel']}'.")
        if sub != "now":
            return reply(
                f"current: {cur}\nlatest:  {rel.version} ({cfg['channel']})\n\n{rel.body.strip()[:1500]}\n\n"
                "Run `/update now` to install it (smoke-tested, auto-rollback; the daemon restarts)."
            )

        if not upd.is_newer(rel.version, upd.current_version()):
            return reply(f"current: {cur}\nAlready up to date (latest is {rel.version}).")
        return reply(await asyncio.to_thread(upd.apply_detached))
