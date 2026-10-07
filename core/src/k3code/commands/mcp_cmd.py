"""/mcp [reload]: MCP servers declared under ``mcp.servers``."""

from __future__ import annotations

from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply, session_cwd, split_args


class McpCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="mcp", help="List MCP servers with status and tool counts; /mcp reload restarts them")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        if args and args[0] == "reload":
            ctx.apply_file_config(session_cwd(ctx, session_id))
            await ctx.mcp.reload(ctx.config.mcp.servers)
        elif args:
            return reply("Usage: /mcp | /mcp reload")
        else:
            await ctx.mcp.ensure_started()
        states = ctx.mcp.states()
        if not states:
            return reply("No MCP servers configured (add them under mcp.servers in config).", servers=[])
        rows = []
        lines = []
        for st in states:
            rows.append(
                {
                    "name": st.name,
                    "transport": st.transport,
                    "status": st.status,
                    "tools": len(st.tools),
                    "error": st.error,
                }
            )
            err = f"  [{st.error}]" if st.error else ""
            lines.append(f"{st.name} ({st.transport}): {st.status}, {len(st.tools)} tool(s){err}")
        return reply(("Reloaded.\n" if args else "") + "\n".join(lines), servers=rows)
