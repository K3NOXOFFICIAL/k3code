"""/mcp [reload | enable <name> | disable <name>]: MCP servers from ``mcp.servers`` and the project's ``.mcp.json``."""

from __future__ import annotations

from typing import Any

from k3code import mcpjson
from k3code.commands import CommandDef
from k3code.commands._util import reply, session_cwd, split_args

USAGE = "Usage: /mcp | /mcp reload | /mcp enable <name> | /mcp disable <name>"


class McpCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="mcp",
            help="List MCP servers with status and tool counts; /mcp reload restarts them; /mcp enable|disable <name> "
            "starts or stops a server from the project's .mcp.json",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        cwd = session_cwd(ctx, session_id)
        note = ""
        if args and args[0] == "reload" and len(args) == 1:
            ctx.apply_file_config(cwd)
            await ctx.mcp.reload(mcpjson.merged(ctx.config.mcp.servers, cwd))
            note = "Reloaded.\n"
        elif args and args[0] in ("enable", "disable") and len(args) == 2:
            name = args[1]
            servers = mcpjson.declared(cwd)
            if name not in servers:
                return reply(f"No server {name!r} in this project's .mcp.json (or the project is not trusted).")
            mcpjson.set_enabled(cwd, name, servers[name] if args[0] == "enable" else None)
            ctx.apply_file_config(cwd)  # the merged server set changed: restarted on ensure_started below
            await ctx.mcp.ensure_started()
            note = f"{name} {args[0]}d.\n"
        elif args:
            return reply(USAGE)
        else:
            await ctx.mcp.ensure_started()
        rows: list[dict[str, Any]] = []
        lines: list[str] = []
        for st in ctx.mcp.states():
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
        _, available = mcpjson.split(cwd)
        for name in available:
            rows.append({"name": name, "transport": "", "status": "available", "tools": 0, "error": ""})
            lines.append(f"{name}: available (from .mcp.json; /mcp enable {name} starts it)")
        if not rows:
            return reply("No MCP servers configured (add them under mcp.servers in config).", servers=[])
        return reply(note + "\n".join(lines), servers=rows)
