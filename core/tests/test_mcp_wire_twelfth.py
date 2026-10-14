"""MCP tools on the wire: a name over 64 characters or a top-level anyOf/oneOf/allOf in an input schema is refused by
the providers, and an activated tool is sent with every later request, so one such tool failed the rest of
the session."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from k3code.extratools import mcp_prompt, register_mcp_tools
from k3code.mcpclient import McpManager, McpToolInfo, _list_all_tools, qualified_name
from k3code.tools import ToolRegistry

LONG = ("Cloudflare_Developer_Platform", "migrate_pages_to_workers_guide")  # 66 characters qualified


def _manager(tools: list[McpToolInfo]) -> McpManager:
    manager = McpManager({})
    manager.tools = lambda: tools  # type: ignore[method-assign]
    return manager


def test_a_long_qualified_name_is_shortened_stably_and_a_short_one_is_left_alone():
    info = McpToolInfo(*LONG, "d", {})
    assert len(qualified_name(*LONG)) == 66 and len(info.qualified) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", info.qualified)
    assert info.qualified == McpToolInfo(*LONG, "d", {}).qualified  # stable between runs
    other = McpToolInfo(LONG[0], LONG[1] + "_2", "d", {})
    assert other.qualified != info.qualified
    assert McpToolInfo("Neon", "run_sql", "d", {}).qualified == "mcp__Neon__run_sql"  # fits: unchanged


def test_a_long_tool_is_registered_activated_found_and_called_by_its_short_name():
    info = McpToolInfo(*LONG, "d", {})
    calls: list[str] = []
    manager = _manager([info])

    async def call(qualified: str, args: dict) -> dict:
        calls.append(qualified)
        return {"content": "ok"}

    manager.call = call  # type: ignore[method-assign]
    manager.servers = {}
    reg = ToolRegistry()
    register_mcp_tools(reg, manager)
    assert manager.find(info.qualified) is info and manager.find(info.full_name) is info
    assert {t.qualified for t in manager.search(f"select:{info.full_name}")} == {info.qualified}
    assert {t.qualified for t in manager.search(f"select:{info.qualified}")} == {info.qualified}
    res = asyncio.run(reg.get("mcp_tool_search")[1]({"query": f"select:{info.qualified}"}))
    assert info.qualified in res["content"]
    assert [s.name for s in reg.specs() if s.name.startswith("mcp__")] == [info.qualified]
    assert all(len(s.name) <= 64 for s in reg.specs())
    asyncio.run(reg.get(info.qualified)[1]({}))
    assert calls == [info.qualified]
    assert info.qualified[len(qualified_name(LONG[0], "")) :] in mcp_prompt(manager)


def test_a_top_level_union_is_flattened_to_an_object_schema():
    variants = [
        {"type": "object", "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["q"]},
        {"type": "object", "properties": {"id": {"type": "string"}, "q": {"type": "string"}}, "required": ["q", "id"]},
    ]
    for key in ("anyOf", "oneOf", "allOf"):
        schema = McpToolInfo("s", "t", "d", {key: variants}).schema
        assert schema["type"] == "object" and set(schema["properties"]) == {"q", "limit", "id"}
        assert schema["required"] == ["q"]  # only what every variant requires
        assert not {"anyOf", "oneOf", "allOf"} & set(schema)
    ref = {
        "oneOf": [{"$ref": "#/$defs/A"}],
        "$defs": {"A": {"type": "object", "properties": {"x": {"type": "integer"}}}},
    }
    flat = McpToolInfo("s", "t", "d", ref).schema
    assert flat["type"] == "object" and flat["properties"] == {"x": {"type": "integer"}} and "$defs" in flat
    assert McpToolInfo("s", "t", "d", {"anyOf": [{"type": "null"}]}).schema == {"type": "object", "properties": {}}
    assert McpToolInfo("s", "t", "d", {"properties": {"a": {}}}).schema["type"] == "object"


def test_a_schema_that_works_is_sent_unchanged():
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/A"}, "c": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "$defs": {"A": {"type": "integer"}},
        "additionalProperties": False,
    }
    assert McpToolInfo("s", "t", "d", dict(schema)).schema == schema


def test_a_tool_that_fails_a_static_check_is_skipped_not_advertised():
    class Session:
        async def list_tools(self, params=None):
            tools = [
                SimpleNamespace(name="", description="", inputSchema={"type": "object"}),
                SimpleNamespace(name="bad", description="", inputSchema={"type": "string"}),
                SimpleNamespace(name="good", description="", inputSchema={"anyOf": [{"properties": {"a": {}}}]}),
            ]
            return SimpleNamespace(tools=tools, nextCursor=None)

    tools = asyncio.run(_list_all_tools(Session(), "srv"))
    assert [t.qualified for t in tools] == ["mcp__srv__good"]
    assert tools[0].schema["type"] == "object"


def test_a_rule_written_with_the_full_long_name_still_applies_to_the_shortened_tool() -> None:
    """A tool name over 64 characters goes by its shortened wire name. A deny rule written with the full name (the
    name the user sees in the server's tool list) must not silently stop matching."""
    from k3code.mcpclient import qualified_name, wire_name
    from k3code.permissions.rules import Rule, evaluate

    full = qualified_name("Cloudflare Developer Platform", "migrate_pages_to_workers_guide_and_more")
    assert len(full) > 64
    short = wire_name(full)
    assert short != full and len(short) <= 64
    deny = Rule(tool=full, pattern="*", action="deny")
    assert evaluate(short, "*", [deny]).action == "deny"  # the call arrives under the wire name
    assert evaluate(full, "*", [deny]).action == "deny"  # and the full name still matches itself
    assert evaluate("mcp__other__tool", "*", [deny], default="ask").action == "ask"  # others are not caught
    prefix = Rule(tool="mcp__Cloudflare_Developer_Platform__*", pattern="*", action="deny")
    assert evaluate(short, "*", [prefix]).action == "deny"  # a glob on the prefix always worked
