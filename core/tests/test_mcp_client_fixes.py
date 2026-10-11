"""MCP client: the streamable-HTTP transport against a real SDK server, paginated tools/list, server names that clash
after sanitising, and what a tool result's text is made of."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer

from k3code.config import McpServerConfig
from k3code.mcpclient import McpManager, _content_text, _list_all_tools


async def serve_http(mcp: MCPServer):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task, f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"


async def test_an_http_server_connects_lists_its_tools_and_answers_calls():
    mcp = MCPServer("web")

    @mcp.tool()
    def shout(text: str) -> str:
        """Upper-case the text."""
        return text.upper()

    server, task, url = await serve_http(mcp)
    manager = McpManager({"web": McpServerConfig(url=url)})
    try:
        await manager.ensure_started()
        (state,) = manager.states()
        assert (state.status, state.error) == ("connected", "")
        assert [t.qualified for t in manager.tools()] == ["mcp__web__shout"]
        assert await manager.call("mcp__web__shout", {"text": "hi"}) == {"content": "HI"}
    finally:
        await manager.close()
        server.should_exit = True
        await task


class Paged:
    """A session whose tools/list answers one tool and a cursor per page."""

    def __init__(self, pages: list[list[str]]) -> None:
        self.pages, self.cursors = pages, []

    async def list_tools(self, *, params=None):
        cursor = params.cursor if params else None
        self.cursors.append(cursor)
        i = int(cursor or 0)
        tools = [SimpleNamespace(name=n, description="", input_schema={}) for n in self.pages[i]]
        return SimpleNamespace(tools=tools, next_cursor=str(i + 1) if i + 1 < len(self.pages) else None)


async def test_every_page_of_tools_list_is_read():
    session = Paged([["a", "b"], ["c"], ["d"]])
    tools = await _list_all_tools(session, "srv")
    assert [t.name for t in tools] == ["a", "b", "c", "d"] and session.cursors == [None, "1", "2"]


async def test_two_tools_with_the_same_qualified_name_keep_the_first():
    tools = await _list_all_tools(Paged([["get.x", "get_x", "other"]]), "srv")
    assert [t.name for t in tools] == ["get.x", "other"]


async def test_a_server_that_never_ends_its_cursors_is_cut_off():
    class Endless:
        async def list_tools(self, *, params=None):
            return SimpleNamespace(tools=[], next_cursor="again")

    assert await _list_all_tools(Endless(), "srv") == []


async def test_a_server_name_that_clashes_after_sanitising_is_refused_not_shadowed():
    manager = McpManager(
        {"my.srv": McpServerConfig(command="x", enabled=False), "my_srv": McpServerConfig(command="x", enabled=False)}
    )
    manager._launch_all()
    by_name = {s.name: s for s in manager.states()}
    assert by_name["my.srv"].status == "disabled"
    assert (
        by_name["my_srv"].status == "failed"
        and "my.srv" in by_name["my_srv"].error
        and "rename" in by_name["my_srv"].error
    )


def block(kind: str, **kw):
    return SimpleNamespace(type=kind, **kw)


@pytest.mark.parametrize(
    ("content", "structured", "expected"),
    [
        ([block("text", text="a"), block("text", text="b")], None, "a\nb"),
        ([block("resource", resource=SimpleNamespace(text="file body", uri="file:///x"))], None, "file body"),
        ([block("resource", resource=SimpleNamespace(uri="file:///x"))], None, "[resource]"),
        ([block("resource_link", uri="file:///y")], None, "[resource: file:///y]"),
        ([block("image")], None, "[image]"),
        ([], {"count": 2}, '{"count": 2}'),
        ([block("text", text="see json")], {"count": 2}, "see json"),
    ],
)
def test_the_text_of_a_tool_result(content, structured, expected):
    assert _content_text(SimpleNamespace(content=content, structured_content=structured)) == expected


async def test_a_call_after_the_server_died_revives_it_within_the_turn(monkeypatch):
    import k3code.mcpclient as mc
    from test_cmd_mcp import fake_cfg

    monkeypatch.setattr(mc, "RESTART_BACKOFF_S", 0.0)
    manager = McpManager({"fake": fake_cfg()})
    try:
        await manager.ensure_started()
        assert "error" in await manager.call("mcp__fake__die", {})
        for _ in range(100):  # the runner notices the broken connection
            runner = manager._runners["fake"]
            if runner.state.status == "failed" or runner.dead.is_set() or runner.task.done():
                break
            await asyncio.sleep(0.05)
        assert await manager.call("mcp__fake__echo", {"text": "again"}) == {"content": "echo:again"}
    finally:
        await manager.close()
