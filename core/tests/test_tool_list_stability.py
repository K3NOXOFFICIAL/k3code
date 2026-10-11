"""The tool list leads every request, so a change in it is a prompt-cache miss for the whole request. These pin what
keeps it still: volatile tools last, MCP activations that survive a server's blip, a search tool that is always
there when a server is configured."""

from __future__ import annotations

from k3code.config import McpServerConfig
from k3code.extratools import mcp_prompt, register_mcp_tools
from k3code.mcpclient import McpManager, McpToolInfo, _Runner
from k3code.providers.types import ToolSpec
from k3code.tools import VOLATILE_TOOLS, ToolRegistry, build_registry, register_todo


def spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=name, parameters={"type": "object", "properties": {}})


async def noop(arguments, *, cwd=None):
    return {"content": ""}


def names(reg: ToolRegistry) -> list[str]:
    return [s.name for s in reg.specs()]


def test_volatile_tools_are_advertised_last_wherever_they_were_registered():
    reg = ToolRegistry()
    for n in ("a", "exit_plan", "b", "schedule_next", "c"):
        reg.register(spec(n), noop)
    reg.register(spec("mcp__x__t"), noop, deferred=True)
    reg.activate(["mcp__x__t"])
    assert names(reg) == ["a", "b", "c", "mcp__x__t", "exit_plan", "schedule_next"]
    assert {"exit_plan", "schedule_next"} == VOLATILE_TOOLS
    # without them the rest of the list is byte for byte what it was
    plain = ToolRegistry()
    for n in ("a", "b", "c"):
        plain.register(spec(n), noop)
    plain.register(spec("mcp__x__t"), noop, deferred=True)
    plain.activate(["mcp__x__t"])
    assert names(plain) == names(reg)[:4]


def test_the_default_registry_puts_exit_plan_at_the_end():
    assert names(build_registry())[-1] == "exit_plan"


def test_an_activation_for_a_tool_that_is_not_registered_yet_is_kept():
    reg = ToolRegistry()
    reg.register(spec("a"), noop)
    reg.activate(["mcp__down__t", "mcp__up__t"])  # its server is down at this turn's start
    assert reg.active_names() == ["mcp__down__t", "mcp__up__t"] and names(reg) == ["a"]
    reg.register(spec("mcp__up__t"), noop, deferred=True)
    reg.register(spec("mcp__down__t"), noop, deferred=True)  # back again
    assert names(reg) == ["a", "mcp__down__t", "mcp__up__t"]  # in the order it was loaded
    reg.activate(["mcp__up__t"])
    assert reg.active_names() == ["mcp__down__t", "mcp__up__t"]


def test_the_search_tool_is_there_while_a_configured_server_is_down():
    down = McpManager({"s": McpServerConfig(command="x")})
    reg = ToolRegistry()
    register_mcp_tools(reg, down)  # nothing connected: no tools, but a server is configured
    assert "mcp_tool_search" in reg.names()
    none = ToolRegistry()
    register_mcp_tools(none, McpManager({}))
    assert "mcp_tool_search" not in none.names()
    off = ToolRegistry()
    register_mcp_tools(off, McpManager({"s": McpServerConfig(command="x", enabled=False)}))
    assert "mcp_tool_search" not in off.names()


def test_a_restarting_server_keeps_the_tools_it_had():
    manager = McpManager({"s": McpServerConfig(command="x")})
    runner = _Runner("s", manager.servers["s"])
    runner.state.tools = [McpToolInfo("s", "go", "d", {})]
    manager._runners["s"] = runner
    assert [t.qualified for t in manager.tools()] == ["mcp__s__go"]
    fresh = _Runner("s", manager.servers["s"])  # what _revive puts in its place: connecting, no tools yet
    manager._runners["s"] = fresh
    assert [t.qualified for t in manager.tools()] == ["mcp__s__go"] and mcp_prompt(manager).endswith("mcp__s__{go}")
    manager.servers["s"] = McpServerConfig(command="x", enabled=False)
    manager._runners["s"] = _Runner("s", manager.servers["s"])
    assert manager.tools() == []


def test_the_mcp_names_are_grouped_per_server():
    manager = McpManager({"a.b": McpServerConfig(command="x"), "c": McpServerConfig(command="x")})
    for server, tools in (("a.b", ["get", "put"]), ("c", ["run"])):
        runner = _Runner(server, manager.servers[server])
        runner.state.tools = [McpToolInfo(server, t, "", {}) for t in tools]
        manager._runners[server] = runner
    text = mcp_prompt(manager)
    assert "mcp__a_b__{get, put}" in text and "mcp__c__{run}" in text
    assert text.count("mcp__a_b__") == 1  # the prefix once per server, not once per tool


async def test_the_todo_result_does_not_echo_the_list_back():
    state: dict = {}
    reg = ToolRegistry()
    register_todo(reg, state)
    _, handler = reg.get("todo")
    todos = [{"content": "write the thing", "status": "completed"}, {"content": "test it", "status": "in_progress"}]
    out = await handler({"todos": todos})
    assert out["content"] == "Todos updated (1/2 done)." and len(out["todos"]) == 2
