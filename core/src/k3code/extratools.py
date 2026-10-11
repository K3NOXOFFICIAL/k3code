"""Tools added to a session's registry beyond the builtins: ``skill``, ``mcp_tool_search``, ``mcp__*``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code import skills as skills_mod
from k3code.mcpclient import McpManager, qualified_name
from k3code.providers.types import ToolSpec
from k3code.tools import ToolRegistry

MAX_SKILL_CHARS = 60_000


def register_skill_tool(reg: ToolRegistry, cwd: Path, roots: list[str]) -> None:
    async def tool_skill(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
        name = str(arguments.get("name") or "").strip()
        query = str(arguments.get("query") or "").strip()
        if name:
            skill = skills_mod.find_skill(name, cwd=base, extra_roots=roots)
            if skill is None:
                near = skills_mod.search(name, base, roots, limit=5)
                hint = f" Did you mean: {', '.join(s.name for s in near)}?" if near else ""
                return {"error": f"Unknown skill: {name}.{hint}"}
            from k3code.learning.curator import record_use

            try:
                text = skill.text()
            except OSError as e:  # deleted or unreadable since discovery: a failed use (the curator flags it)
                record_use(skill.name, ok=False)
                return {"error": f"Skill {skill.name} could not be read: {e}"}
            record_use(skill.name, ok=bool(text.strip()))  # an empty SKILL.md gave the model nothing
            if not text.strip():
                return {"error": f"Skill {skill.name} is empty ({skill.path})"}
            if len(text) > MAX_SKILL_CHARS:
                text = text[:MAX_SKILL_CHARS] + "\n…(truncated)"
            return {"content": f"# Skill: {skill.name}\n(location: {skill.path.parent})\n\n{text}"}
        if query:
            hits = skills_mod.search(query, base, roots)
            return {"content": "\n".join(f"- {s.name}: {s.description}" for s in hits) or "No matching skills."}
        return {"error": "skill needs `name` (load) or `query` (search)"}

    base = cwd
    reg.register(
        ToolSpec(
            name="skill",
            description="Load a skill's full instructions by name, or search skills with `query`.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}, "query": {"type": "string"}},
            },
            side_effect=False,
        ),
        tool_skill,
    )


def _arguments(schema: dict[str, Any]) -> str:
    """``a, b?``: a tool's argument names, optional ones marked."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    return ", ".join(k if k in required else f"{k}?" for k in props) if isinstance(props, dict) else ""


def register_mcp_tools(reg: ToolRegistry, mcp: McpManager) -> None:
    """Register every connected MCP tool (schema deferred) plus ``mcp_tool_search``. The search tool is there whenever a
    server is configured, connected or not: its coming and going with a server's blip rewrote the tool list, a
    prompt-cache miss for the whole request each way."""
    tools = mcp.tools()
    if not tools and not any(cfg.enabled for cfg in mcp.servers.values()):
        return

    def make_handler(qualified: str, parallel: bool) -> Any:
        async def handler(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
            return await mcp.call(qualified, arguments)

        if parallel:  # the server is declared read-only (`parallel_safe`): AgentLoop may batch its calls
            handler.parallel_when = lambda _arguments: True  # type: ignore[attr-defined]
        return handler

    for t in tools:
        server = mcp.servers.get(t.server)
        reg.register(
            ToolSpec(
                name=t.qualified,
                description=t.description,
                parameters=t.schema or {"type": "object", "properties": {}},
                side_effect=True,
            ),
            make_handler(t.qualified, bool(server and server.parallel_safe)),
            deferred=True,
        )

    async def tool_search(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"error": "mcp_tool_search needs a `query` (tool names or keywords)"}
        hits = mcp.search(query)
        reg.activate([t.qualified for t in hits])
        if not hits:
            return {"content": "No matching MCP tools."}
        # the schemas and descriptions reach the model through the tool list from the next step on: repeating them here
        # doubled their tokens (about 900 for six tools with ordinary descriptions), so the result names each tool with
        # its arguments
        body = "\n".join(f"- {t.qualified}({_arguments(t.schema)})" for t in hits)
        return {"content": f"{body}\n\nThese tools can now be called directly."}

    reg.register(
        ToolSpec(
            name="mcp_tool_search",
            description=(
                "Load full schemas of deferred MCP tools by exact name (mcp__server__tool) or keywords; "
                "they become callable."
            ),
            parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            side_effect=False,
        ),
        tool_search,
    )


def mcp_prompt(mcp: McpManager, limit: int = 150) -> str:
    """The deferred MCP tools by name, grouped per server: ``mcp__Neon__{run_sql, list_projects}`` instead of every
    ``mcp__Neon__`` repeated (about 40% fewer tokens for 145 tools). The qualified name is the prefix + the name."""
    tools = mcp.tools()
    if not tools:
        return ""
    groups: dict[str, list[str]] = {}
    for t in tools[:limit]:
        prefix = qualified_name(t.server, "")
        groups.setdefault(prefix, []).append(t.qualified[len(prefix) :])
    shown = "\n".join(f"{prefix}{{{', '.join(names)}}}" for prefix, names in groups.items())
    more = f"\n…(+{len(tools) - limit} more)" if len(tools) > limit else ""
    return (
        "## MCP tools (deferred)\n\n"
        "These tools exist but their schemas are not loaded yet. Call `mcp_tool_search` with a name or "
        "keywords to load schemas, then call the tool by its full name (the prefix with one of its names).\n\n"
        f"{shown}{more}"
    )
