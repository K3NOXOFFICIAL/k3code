"""/mcp: stdio MCP client against a fake in-process-launched server, deferred schemas, permissions."""

from __future__ import annotations

import sys
from pathlib import Path

from k3code.config import McpServerConfig
from k3code.extratools import mcp_prompt, register_mcp_tools
from k3code.mcpclient import McpManager, qualified_name
from k3code.permissions import decide
from k3code.tools import build_registry
from m1cmd_helpers import cmd, make_server, new_session, submit_and_wait

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def fake_cfg(**kw) -> McpServerConfig:
    return McpServerConfig(command=sys.executable, args=[FAKE], **kw)


async def test_manager_lists_calls_and_searches():
    mgr = McpManager({"fake": fake_cfg(), "broken": McpServerConfig(command="/nonexistent/binary-xyz")})
    try:
        await mgr.ensure_started()
        states = {s.name: s for s in mgr.states()}
        assert states["fake"].status == "connected" and len(states["fake"].tools) == 2
        assert states["broken"].status == "failed" and states["broken"].error
        names = sorted(t.qualified for t in mgr.tools())
        assert names == ["mcp__fake__add", "mcp__fake__echo"]
        assert qualified_name("my server", "a.b") == "mcp__my_server__a_b"

        assert await mgr.call("mcp__fake__echo", {"text": "hi"}) == {"content": "echo:hi"}
        assert (await mgr.call("mcp__fake__add", {"a": 2, "b": 3}))["content"] == "5"
        assert "error" in await mgr.call("mcp__fake__nope", {})
        bad = await mgr.call("mcp__fake__add", {"a": "x"})  # schema violation → error result, not a crash
        assert "error" in bad

        hits = mgr.search("add integers")
        assert hits and hits[0].qualified == "mcp__fake__add"
        assert mgr.search("mcp__fake__echo")[0].qualified == "mcp__fake__echo"

        prompt = mcp_prompt(mgr)
        assert "mcp__fake__echo" in prompt and "input_schema" not in prompt and "Echo the text" not in prompt
    finally:
        await mgr.close()


async def test_deferred_registration_and_tool_search():
    mgr = McpManager({"fake": fake_cfg()})
    try:
        await mgr.ensure_started()
        reg = build_registry()
        register_mcp_tools(reg, mgr)
        advertised = {s.name for s in reg.specs()}
        assert "mcp_tool_search" in advertised and "mcp__fake__add" not in advertised  # deferred

        _, search = reg.get("mcp_tool_search")
        res = await search({"query": "mcp__fake__add"})
        assert '"a"' in res["content"] and "input_schema" in res["content"]  # full schema on demand
        assert "mcp__fake__add" in {s.name for s in reg.specs()}  # now advertised
        assert "mcp__fake__echo" not in {s.name for s in reg.specs()}

        _, call = reg.get("mcp__fake__echo")  # callable even before its schema was loaded
        assert (await call({"text": "z"}))["content"] == "echo:z"
        assert "error" in await search({})
    finally:
        await mgr.close()


def test_mcp_tools_default_to_ask_and_match_rules():
    assert decide(mode="default", tool="mcp__fake__add").action == "ask"
    assert decide(mode="plan", tool="mcp__fake__add").action == "deny"
    from k3code.permissions import from_config

    rules = from_config({"mcp__fake__*": "allow"})
    assert decide(mode="default", tool="mcp__fake__add", user_rules=rules).action == "allow"
    assert decide(mode="default", tool="mcp__other__add", user_rules=rules).action == "ask"
    assert decide(mode="default", tool="mcp_tool_search").action == "allow"


async def test_mcp_command_status_reload_and_turn(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    assert "No MCP servers configured" in (await cmd(server, "/mcp", sid))["output"]

    server.config.mcp.servers = {"fake": fake_cfg()}
    server.mcp.configure(server.config.mcp.servers)
    try:
        res = await cmd(server, "/mcp", sid)
        assert "fake (stdio): connected, 2 tool(s)" in res["output"]
        assert res["servers"][0]["tools"] == 2

        await submit_and_wait(server, "go")
        assert "mcp__fake__add" in provider.seen[0][0].content  # names in prompt, deferred
        loop = server.session.loop
        assert "mcp__fake__add" in loop.tools.names() and "mcp_tool_search" in loop.tool_specs().__repr__()
        assert "mcp__fake__add" not in {s.name for s in loop.tool_specs()}

        res = await cmd(server, "/mcp reload", sid)
        assert res["output"].startswith("Reloaded.") and "connected" in res["output"]
        assert "Usage" in (await cmd(server, "/mcp bogus", sid))["output"]
    finally:
        await server.close()
