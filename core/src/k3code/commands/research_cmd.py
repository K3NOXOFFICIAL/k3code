"""/ultraresearch <question>: cited multi-source research report."""

from __future__ import annotations

import shlex
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply
from k3code.research.flow import ResearchUnavailable


class UltraResearchCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="ultraresearch", help="Cited research report: /ultraresearch [--n N] <question>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return reply("No active session.")
        n: int | None = None
        parts = shlex.split(arg) if arg else []
        if len(parts) >= 2 and parts[0] == "--n" and parts[1].isdigit():
            n = int(parts[1])
            arg = arg.split(None, 2)[2] if len(parts) > 2 else ""
        if not arg.strip():
            return reply("Usage: /ultraresearch [--n N] <question>")
        question = arg.strip()
        tools = ctx.research.tools()
        if (why := await tools.unavailable_reason()):
            return reply(f"/ultraresearch is unavailable: {why}. Connect an MCP web-search server (e.g. "
                         "hub_searxng) or set research.searxng_url.")

        async def job() -> str:
            try:
                res = await ctx.research.run(live, question, n_sub=n)
            except ResearchUnavailable as e:
                return str(e)
            return f"{res.report}\n\nReport saved: {res.path}"

        try:
            ctx.start_job(live, f"/ultraresearch {question}", job)
        except Exception as e:  # noqa: BLE001
            return reply(str(e))
        return reply(f"Researching with {tools.name}: decompose → search → read → cross-check → synthesize.")
