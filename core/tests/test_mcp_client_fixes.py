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


def test_mcp_search_reads_the_notations_the_system_prompt_prints():
    from k3code.mcpclient import McpToolInfo

    tools = [
        McpToolInfo("Neon", "run_sql", "Run a SQL statement", {}),
        McpToolInfo("Neon", "list_projects", "List projects", {}),
        McpToolInfo("github", "get_me", "The signed-in user", {}),
    ]
    manager = McpManager({})
    manager.tools = lambda: tools  # type: ignore[method-assign]

    def names(query: str) -> set[str]:
        return {t.qualified for t in manager.search(query)}

    both = {"mcp__Neon__run_sql", "mcp__Neon__list_projects"}
    assert names("mcp__Neon__{run_sql, list_projects}") == both  # as the prompt prints them
    assert names("select:mcp__Neon__run_sql,mcp__Neon__list_projects") == both  # both, not the last one only
    assert names("select:mcp__Neon__run_sql") == {"mcp__Neon__run_sql"}
    assert names("mcp__Neon__run_sql mcp__Neon__list_projects") == both
    assert names("run_sql") == {"mcp__Neon__run_sql"}
    assert names("zzzz qqqq") == set()


def test_mcp_search_by_name_activates_those_tools_and_no_others():
    """``select:search`` activated eight tools and ``get_issue`` brought in ``get_issue_comments``: every activated
    schema is sent with each later request, and the tool list change rewrites the prompt cache."""
    from k3code.mcpclient import McpToolInfo

    tools = [
        McpToolInfo("gh", "get_issue", "Get an issue", {}),
        McpToolInfo("gh", "get_issue_comments", "Get the comments of an issue", {}),
        McpToolInfo("gh", "get_issue_labels", "Get the labels of an issue", {}),
        McpToolInfo("gh", "search_code", "Search code", {}),
        McpToolInfo("fs", "search", "Search files by name", {}),
        McpToolInfo("web", "search", "Search the web", {}),
        McpToolInfo("fs", "read_file", "Read a file", {}),
        McpToolInfo("fs", "write_file", "Write a file", {}),
    ]
    manager = McpManager({})
    manager.tools = lambda: tools  # type: ignore[method-assign]

    def names(query: str) -> set[str]:
        return {t.qualified for t in manager.search(query)}

    assert names("select:search") == {"mcp__fs__search", "mcp__web__search"}  # not search_code, not a description hit
    assert names("select:read_file,write_file") == {"mcp__fs__read_file", "mcp__fs__write_file"}
    assert names("select:read,write") == set()  # names only: no tool is called just `read`
    for query in ("select:mcp__gh__get_issue", "mcp__gh__{get_issue}", "mcp__gh__get_issue"):
        assert names(query) == {"mcp__gh__get_issue"}, query
    # keywords still search the descriptions, and a qualified name next to them keeps its own exactness
    assert names("comments") == {"mcp__gh__get_issue_comments"}
    assert names("mcp__gh__get_issue labels") == {"mcp__gh__get_issue", "mcp__gh__get_issue_labels"}


def test_mcp_search_select_takes_the_short_name_the_prompt_prints_for_a_sanitized_tool():
    """``qualified_name`` turns a character outside [A-Za-z0-9_-] into ``_``, and the prompt lists the tool by that
    form (``mcp__docs__{search_docs}``): ``select:search_docs`` found nothing for the server's ``search.docs``."""
    from k3code.mcpclient import McpToolInfo

    tools = [
        McpToolInfo("docs", "search.docs", "Search the docs", {}),
        McpToolInfo("fs", "files/read", "Read a file", {}),
        McpToolInfo("gh", "get_issue", "Get an issue", {}),
        McpToolInfo("gh", "get_issue_comments", "Get the comments of an issue", {}),
    ]
    manager = McpManager({})
    manager.tools = lambda: tools  # type: ignore[method-assign]

    def names(query: str) -> set[str]:
        return {t.qualified for t in manager.search(query)}

    assert names("select:search_docs") == {"mcp__docs__search_docs"}
    assert names("select:files_read") == {"mcp__fs__files_read"}
    assert names("select:search.docs") == {"mcp__docs__search_docs"}  # the server's own name still works
    assert names("select:get_issue") == {"mcp__gh__get_issue"}  # the short name is exact, not a prefix


def test_mcp_search_notation_expands_in_linear_time():
    """The lazy ``\\S*?`` prefix was rescanned from every position: 20,000 characters without a space took seconds."""
    import time

    from k3code.mcpclient import _expand_names

    assert _expand_names("mcp__Neon__{run_sql, list_projects}") == "mcp__neon__run_sql, mcp__neon__list_projects"
    assert _expand_names("select:a{b,c}d {e}") == "ab, acd e"
    for text in ("a" * 40_000, "a" * 40_000 + "{", "{a" * 20_000):
        start = time.monotonic()
        _expand_names(text)
        assert time.monotonic() - start < 1.0, text[:3]  # about 9 s before, a few ms after
