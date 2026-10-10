"""/ultraresearch <question>: cited multi-source research report."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply, split_args
from k3code.commands.ultra_cmd import JobSpec, start_spec, with_hook_context
from k3code.research.flow import ResearchUnavailable


class UltraResearchCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="ultraresearch", help="Cited research report: /ultraresearch [--n N] <question>")

    async def prepare(self, ctx: Any, live: Any, arg: str, *, context: str = "") -> JobSpec | dict[str, Any]:
        n: int | None = None
        parts = split_args(arg) if arg else []  # an apostrophe in the question must not fail the command
        if len(parts) >= 2 and parts[0] == "--n" and parts[1].isdigit():
            n = int(parts[1])
            arg = arg.split(None, 2)[2] if len(parts) > 2 else ""
        if not arg.strip():
            return reply("Usage: /ultraresearch [--n N] <question>")
        question = arg.strip()
        tools = ctx.research.tools()
        if why := await tools.unavailable_reason():
            return reply(
                f"/ultraresearch is unavailable: {why}. Connect an MCP web-search server (e.g. "
                "hub_searxng) or set research.searxng_url."
            )

        async def job() -> str:
            try:
                res = await ctx.research.run(live, with_hook_context(question, context), n_sub=n)
            except ResearchUnavailable as e:
                return str(e)
            return f"{res.report}\n\nReport saved: {res.path}"

        return JobSpec(
            f"/ultraresearch {question}",
            job,
            f"Researching with {tools.name}: decompose → search → read → cross-check → synthesize.",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return reply("No active session.")
        return start_spec(ctx, live, await self.prepare(ctx, live, arg))
