"""Tools added to a session's registry beyond the builtins: ``skill``, ``mcp_tool_search``, ``mcp__*``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from k3code import skills as skills_mod
from k3code.mcpclient import McpManager
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


def register_mcp_tools(reg: ToolRegistry, mcp: McpManager) -> None:
    """Register every connected MCP tool (schema deferred) plus ``mcp_tool_search``."""
    tools = mcp.tools()
    if not tools:
        return

    def make_handler(qualified: str) -> Any:
        async def handler(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
            return await mcp.call(qualified, arguments)

        return handler

    for t in tools:
        reg.register(
            ToolSpec(
                name=t.qualified,
                description=t.description,
                parameters=t.schema or {"type": "object", "properties": {}},
                side_effect=True,
            ),
            make_handler(t.qualified),
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
        body = [{"name": t.qualified, "description": t.description, "input_schema": t.schema} for t in hits]
        return {"content": json.dumps(body, indent=2) + "\n\nThese tools can now be called directly."}

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
    tools = mcp.tools()
    if not tools:
        return ""
    names = [t.qualified for t in tools]
    shown = ", ".join(names[:limit]) + (f", …(+{len(names) - limit} more)" if len(names) > limit else "")
    return (
        "## MCP tools (deferred)\n\n"
        "These tools exist but their schemas are not loaded yet. Call `mcp_tool_search` with a name or "
        f"keywords to load schemas, then call the tool.\n\n{shown}"
    )
