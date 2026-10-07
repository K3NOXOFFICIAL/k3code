"""/mcp: stdio MCP client against a fake in-process-launched server, deferred schemas, permissions."""

from __future__ import annotations

import asyncio
import contextlib
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
        assert states["fake"].status == "connected" and len(states["fake"].tools) == 4
        assert states["broken"].status == "failed" and states["broken"].error
        names = sorted(t.qualified for t in mgr.tools())
        assert names == ["mcp__fake__add", "mcp__fake__die", "mcp__fake__echo", "mcp__fake__sleep"]
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
        assert "fake (stdio): connected, 4 tool(s)" in res["output"]
        assert res["servers"][0]["tools"] == 4

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


# ── regressions from the long-run audit: one MCP runner serves every session ──


async def test_a_cancelled_call_does_not_kill_the_server_for_everyone():
    """/stop cancels the caller; the runner then resolved a cancelled future (InvalidStateError), exited, and the stdio
    subprocess died: every later call from every session failed until `/mcp reload`."""
    mgr = McpManager({"fake": fake_cfg()})
    try:
        await mgr.ensure_started()
        task = asyncio.create_task(mgr.call("mcp__fake__sleep", {"seconds": 1.0}))
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.2)  # the abandoned call finishes server-side
        assert (await mgr.call("mcp__fake__echo", {"text": "still alive"})) == {"content": "echo:still alive"}
        assert [s.status for s in mgr.states()] == ["connected"]
    finally:
        await mgr.close()


async def test_a_slow_call_does_not_block_other_sessions():
    mgr = McpManager({"fake": fake_cfg()})
    try:
        await mgr.ensure_started()
        slow = asyncio.create_task(mgr.call("mcp__fake__sleep", {"seconds": 1.5}))
        await asyncio.sleep(0.2)
        started = asyncio.get_running_loop().time()
        assert (await mgr.call("mcp__fake__echo", {"text": "fast"})) == {"content": "echo:fast"}
        assert asyncio.get_running_loop().time() - started < 1.0  # not queued behind the sleeping call
        assert (await slow) == {"content": "slept"}
    finally:
        await mgr.close()


async def test_a_dead_server_is_restarted_on_a_later_turn(monkeypatch):
    """ensure_started() returned as soon as it had run once: a server that crashed (or was down at daemon boot) stayed
    'connected'/'failed' until `/mcp reload`."""
    import k3code.mcpclient as mc

    monkeypatch.setattr(mc, "RESTART_BACKOFF_S", 0.0)
    mgr = McpManager({"fake": fake_cfg()})
    try:
        await mgr.ensure_started()
        crashed = await mgr.call("mcp__fake__die", {})
        assert "error" in crashed
        for _ in range(100):  # the runner notices the broken connection
            if mgr._runners["fake"].state.status == "failed" or mgr._runners["fake"].task.done():
                break
            await asyncio.sleep(0.05)
        await mgr.ensure_started()  # the next turn of any session
        assert [s.status for s in mgr.states()] == ["connected"]
        assert (await mgr.call("mcp__fake__echo", {"text": "back"})) == {"content": "echo:back"}
    finally:
        await mgr.close()


async def test_ensure_started_is_single_flight():
    mgr = McpManager({"fake": fake_cfg()})
    try:
        await asyncio.gather(*(mgr.ensure_started() for _ in range(8)))
        assert len(mgr._runners) == 1
        assert len([r for r in mgr._runners.values() if r.task and not r.task.done()]) == 1
    finally:
        await mgr.close()
