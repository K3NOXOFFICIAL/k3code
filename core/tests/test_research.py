"""/ultraresearch with fake search/fetch tools: the report's citations match the sources."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import httpx
import respx

from k3code.research.flow import finalize_report, loose_json
from k3code.research.state import ResearchState
from k3code.research.tools import (
    BuiltinTools,
    Hit,
    McpTools,
    ResearchTools,
    extract_text,
    parse_search_text,
    pick_tools,
)
from test_autonomy_gateway import call, events, make

PAGES = {
    "https://a.example/1": ("Alpha Page", "Alpha facts: the sky is blue."),
    "https://b.example/2": ("Beta Page", "Beta facts: the sky is blue too."),
    "https://c.example/3": ("Gamma Page", "Gamma facts: grass is green."),
}


class FakeTools(ResearchTools):
    name = "fake tools"

    def __init__(self) -> None:
        self.searches: list[str] = []
        self.fetched: list[str] = []

    async def search(self, query, n=5):
        self.searches.append(query)
        return {
            "sky color": [Hit("A", "https://a.example/1", "snip a"), Hit("B", "https://b.example/2", "snip b")],
            "grass color": [Hit("C", "https://c.example/3", "snip c"), Hit("A again", "https://a.example/1")],
        }.get(query, [])

    async def fetch(self, url):
        self.fetched.append(url)
        return PAGES[url]


PLAN = {
    "sub_topics": [
        {"name": "sky", "initial_queries": ["sky color"]},
        {"name": "grass", "initial_queries": ["grass color"]},
    ]
}
STEPS = [
    {"type": "text", "match": "You are a research planner", "text": json.dumps(PLAN)},
    {
        "type": "text",
        "match": "Source: https://a.example/1",
        "text": json.dumps({"claims": ["The sky is blue."], "new_questions": []}),
    },
    {
        "type": "text",
        "match": "Source: https://b.example/2",
        "text": json.dumps({"claims": ["Sky looks blue to observers."], "new_questions": []}),
    },
    {
        "type": "text",
        "match": "Source: https://c.example/3",
        "text": json.dumps({"claims": ["Grass is green."], "new_questions": []}),
    },
    {
        "type": "text",
        "match": "You cross-check claims",
        "text": json.dumps({"corroborated": ["c0", "c1"], "contradictions": []}),
    },
    {
        "type": "text",
        "match": "You are a research writer",
        "text": "The sky is blue [S1, S2, S9]. Grass is green [S3].\nUnsourced guess [S7].\n\n"
        "## Sources\n- [S1]: WRONG TITLE — http://wrong",
    },
]


def research_server(tmp_path, monkeypatch, tools):
    server = make(tmp_path, monkeypatch, STEPS, mode="auto", autonomy={"plan_first": False, "proposals": False})
    server.research_tools = tools
    return server


async def test_ultraresearch_report_citations_match_sources(tmp_path, monkeypatch):
    tools = FakeTools()
    server = research_server(tmp_path, monkeypatch, tools)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    out = await call(server, "slash.exec", {"command": "ultraresearch --n 2 What colour are the sky and grass?"})
    assert "Researching with fake tools" in out["output"]
    await server.session.turn_task
    report = server.session.stored.messages[-1]["content"]
    # invalid ids were dropped, valid ones kept
    assert "[S1, S2]" in report and "S9" not in report and "S7" not in report
    # Sources section rebuilt from the registry, in id order, matching what the tools returned
    sources = report.split("## Sources\n")[1].split("\n\nReport saved")[0].splitlines()
    assert sources == [
        "- [S1]: Alpha Page — https://a.example/1",
        "- [S2]: Beta Page — https://b.example/2",
        "- [S3]: Gamma Page — https://c.example/3",
    ]
    assert "WRONG TITLE" not in report
    # every cited id has exactly one source line
    cited = {i for m in re.findall(r"\[(S\d+(?:, S\d+)*)\]", report.split("## Sources")[0]) for i in m.split(", ")}
    assert cited == {"S1", "S2", "S3"}
    # parallel search/dedupe: the duplicate URL was fetched once
    assert sorted(tools.searches) == ["grass color", "sky color"] and tools.fetched.count("https://a.example/1") == 1
    # file + artifact
    files = list((tmp_path / ".k3code" / "research").glob("*-what-colour-are-the-sky-and-grass.md"))
    assert len(files) == 1 and "## Sources" in files[0].read_text()
    assert server.artifacts.list(kind="research")[0].path == str(files[0].resolve())
    assert [e["phase"] for e in events(server, "research.progress")] == [
        "decomposing",
        "searching",
        "reading",
        "cross-checking",
        "synthesizing",
    ]


async def test_ultraresearch_tiers_and_writer_sees_crosscheck_flags(tmp_path, monkeypatch):
    server = research_server(tmp_path, monkeypatch, FakeTools())
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await call(server, "slash.exec", {"command": "ultraresearch colours"})
    await server.session.turn_task
    calls = [c for p in server.providers for c in p.log]
    by = {
        k: c
        for k in ("research planner", "research analyst", "cross-check", "research writer")
        for c in calls
        if k in c["text"]
    }
    assert by["research planner"]["model"] == "m-cheap" and by["research analyst"]["model"] == "m-cheap"
    assert by["cross-check"]["model"] == "m-cheap"
    assert by["research writer"]["model"] == "m-strong"  # synthesis on the strong tier
    order = [
        next(i for i, c in enumerate(calls) if k in c["text"])
        for k in ("research planner", "research analyst", "cross-check", "research writer")
    ]
    assert order == sorted(order)
    writer = by["research writer"]["text"]
    assert "[S1] (corroborated) The sky is blue." in writer and "[S3] (single source) Grass is green." in writer


async def test_ultraresearch_clear_message_when_search_unavailable(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], mode="auto", research={"searxng_url": "http://127.0.0.1:9"})
    await call(server, "session.create", {"cwd": str(tmp_path)})
    out = await call(server, "slash.exec", {"command": "ultraresearch anything"})
    assert "unavailable" in out["output"] and "unreachable" in out["output"] and "research.searxng_url" in out["output"]
    server.config.research = {"searxng_url": ""}
    out = await call(server, "slash.exec", {"command": "ultraresearch anything"})
    assert "no SearXNG URL configured" in out["output"]
    out = await call(server, "slash.exec", {"command": "ultraresearch"})
    assert "Usage" in out["output"]


@respx.mock
async def test_builtin_tools_against_a_mocked_searxng_and_pages(tmp_path):
    respx.get("http://searx.test/search").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "results": [
                    {"title": "T1", "url": "http://p.test/1", "content": "snippet one"},
                    {"title": "T2", "url": "http://p.test/2"},
                ]
            },
        )
    )
    html_doc = (
        "<html><head><title>Real   Title</title><script>var x=1;</script></head><body><nav>menu</nav>"
        "<main><h1>Heading</h1><p>Body text one.</p><p>Body text two.</p></main><footer>foot</footer></body></html>"
    )
    respx.get("http://p.test/1").mock(
        return_value=httpx.Response(200, text=html_doc, headers={"content-type": "text/html"})
    )
    tools = BuiltinTools("http://searx.test")
    assert await tools.unavailable_reason() == ""
    hits = await tools.search("anything", 5)
    assert [h.url for h in hits] == ["http://p.test/1", "http://p.test/2"] and hits[0].snippet == "snippet one"
    title, text = await tools.fetch("http://p.test/1")
    assert title == "Real Title" and "Body text one." in text and "menu" not in text and "var x" not in text


def test_extract_text_prefers_main_and_strips_noise():
    body = "word " * 120
    title, text = extract_text(
        f"<html><title>T</title><body><div>sidebar junk</div><article><p>{body}</p></article>"
        "<style>.a{}</style></body></html>"
    )
    assert title == "T" and "sidebar" not in text and "style" not in text and text.startswith("word")
    assert extract_text("<p>plain &amp; simple</p>")[1] == "plain & simple"


def test_parse_search_text_json_and_plain():
    j = json.dumps({"results": [{"title": "X", "url": "http://x.test", "content": "c"}]})
    assert parse_search_text(j) == [Hit("X", "http://x.test", "c")]
    plain = "1. Title One - http://one.test/page.\n2. Two http://two.test"
    assert [h.url for h in parse_search_text(plain)] == ["http://one.test/page", "http://two.test"]


async def test_pick_tools_prefers_mcp_search_and_fetch():
    class FakeMcp:
        def __init__(self, tools):
            self._t = tools
            self.calls = []

        def tools(self):
            return self._t

        async def call(self, q, args):
            self.calls.append((q, args))
            if "search" in q:
                return {"content": json.dumps({"results": [{"title": "H", "url": "http://h.test", "content": ""}]})}
            return {"content": "fetched text"}

    def tool(name):
        return SimpleNamespace(
            name=name, qualified=f"mcp__k3nox__{name}", schema={"properties": {"query": {}, "url": {}}}
        )

    mcp = FakeMcp([tool("hub_searxng__search"), tool("hub_fetch__fetch")])
    picked = pick_tools(SimpleNamespace(research={}), mcp)
    assert isinstance(picked, McpTools) and "hub_searxng__search" in picked.name
    assert [h.url for h in await picked.search("q")] == ["http://h.test"]
    assert await picked.fetch("http://h.test") == ("http://h.test", "fetched text")
    assert mcp.calls[0][1] == {"query": "q"} and mcp.calls[1][1] == {"url": "http://h.test"}
    assert isinstance(pick_tools(SimpleNamespace(research={}), FakeMcp([])), BuiltinTools)


def test_finalize_report_and_loose_json():
    st = ResearchState("q")
    st.register_source("http://a", "A")
    st.register_source("http://b", "B")
    out, cited = finalize_report("Fact [S2]. Bad [S5]. Two [S1, S4].\n\n## Sources\n- junk", st)
    assert cited == ["S1", "S2"] and "[S5]" not in out and "[S1]" in out and "junk" not in out
    assert out.endswith("- [S1]: A — http://a\n- [S2]: B — http://b")
    none, cited = finalize_report("No cites here.", st)
    assert cited == [] and "no verifiable citations" in none
    assert loose_json('prefix {"a": 1} suffix') == {"a": 1} and loose_json("zzz") is None


async def test_pick_tools_ignores_non_web_search_decoys():
    def tool(name):
        return SimpleNamespace(name=name, qualified=f"mcp__k3nox__{name}", schema={})

    decoys = [tool("search_memory"), tool("fleet_sessions_search"), tool("nc_search_files"), tool("fleet_fetch"),
              tool("mcp_tool_search")]
    mcp = SimpleNamespace(tools=lambda: [*decoys, tool("hub_searxng__search"), tool("hub_fetch__fetch")])
    picked = pick_tools(SimpleNamespace(research={}), mcp)
    assert isinstance(picked, McpTools)
    assert picked.search_tool.name == "hub_searxng__search" and picked.fetch_tool.name == "hub_fetch__fetch"
    # decoys alone: no web tool at all -> built-ins
    assert isinstance(pick_tools(SimpleNamespace(research={}), SimpleNamespace(tools=lambda: decoys)), BuiltinTools)
    # a generic web search tool is still found when no searxng marker exists
    generic = SimpleNamespace(tools=lambda: [*decoys, tool("brave_search")])
    assert pick_tools(SimpleNamespace(research={}), generic).search_tool.name == "brave_search"


async def test_fetch_page_reads_a_capped_prefix_not_the_whole_body():
    """client.get() buffered the entire response in the shared daemon before truncating the text."""
    import httpx

    from k3code.research import tools

    served = {"bytes": 0}

    async def body():
        chunk = b"<p>" + b"x" * 65_000 + b"</p>"
        for _ in range(2000):  # 130 MB if it were ever read to the end
            served["bytes"] += len(chunk)
            yield chunk

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        title, text = await tools.fetch_page("https://example.com/huge", client=client)
    assert len(text) <= tools.MAX_FETCH_CHARS and text.startswith("x")
    assert served["bytes"] < 6 * tools.MAX_FETCH_BYTES  # stopped reading soon after the cap
