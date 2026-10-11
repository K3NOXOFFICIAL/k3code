"""/ultraresearch and the web tools: the failures a real run hit that the fake-tool tests did not."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from k3code.paths import GENERATED_DIRS, ignore_generated
from k3code.research.browser import Verdict, classify
from k3code.research.flow import spread_by_topic
from k3code.research.state import Learning
from k3code.research.tools import BuiltinTools, McpTools, extract_text, fetch_page, pick_tools
from test_autonomy_gateway import call, make
from test_research import STEPS, FakeTools


def tool(server: str, name: str, schema: dict | None = None):
    return SimpleNamespace(name=name, qualified=f"mcp__{server}__{name}", schema=schema or {}, description="")


def picked(*tools):
    return pick_tools(SimpleNamespace(research={}), SimpleNamespace(tools=lambda: list(tools)))


def test_search_tools_of_other_services_are_not_the_web_search():
    others = [
        tool("github", "search_issues"),
        tool("Gmail", "search_threads"),
        tool("Neon", "search"),
        tool("Google_Calendar", "search_events"),
    ]
    assert isinstance(picked(*others), BuiltinTools)
    web = picked(*others, tool("Tavily", "tavily_search"))
    assert isinstance(web, McpTools) and web.search_tool.name == "tavily_search"
    # "profile" is not "file", so the server is not mistaken for a file tool
    assert picked(tool("profile_tools", "web_search")).search_tool.name == "web_search"
    # the reference fetch server: its tool takes a url
    fetch = picked(
        tool("github", "search_code"), tool("fetch", "fetch", {"properties": {"url": {}}}), tool("w", "web_search")
    )
    assert fetch.fetch_tool.name == "fetch"


def test_a_login_phrase_in_a_script_or_a_long_page_is_not_a_login_wall():
    filler = "<p>" + "word " * 400 + "</p>"
    script = "<script>var t = {msg: 'You must be logged in to comment'}</script>"
    assert classify(200, f"<html><body>{filler}{script}</body></html>") is Verdict.OK
    assert (
        classify(200, f"<html><body>{filler}<p>Note: you must be logged in to comment.</p></body></html>") is Verdict.OK
    )
    # a short page that says it, or a 401, is a wall
    assert classify(200, "<html><body><p>Please log in to continue.</p></body></html>") is Verdict.LOGIN
    assert classify(401, f"<html><body>{filler}</body></html>") is Verdict.LOGIN


def test_extract_text_keeps_a_form_wrapped_body_and_an_article_header():
    page = "<html><body><form id='f'><div><p>Body inside the form.</p></div></form></body></html>"
    assert "Body inside the form." in extract_text(page)[1]
    article = (
        "<html><body><article><header><h1>The Title</h1><p>By Ann</p></header><p>Text.</p></article></body></html>"
    )
    assert "The Title" in extract_text(article)[1] and "By Ann" in extract_text(article)[1]


@respx.mock
async def test_fetch_page_refuses_binary_bodies_and_reads_meta_charset():
    respx.get("https://h.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://h.test/a.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 \xff\xfe", headers={"content-type": "application/pdf"})
    )
    html = "<html><head><meta charset=iso-8859-1><title>t</title></head><body><p>café crème</p></body></html>"
    respx.get("https://h.test/latin").mock(
        return_value=httpx.Response(200, content=html.encode("latin-1"), headers={"content-type": "text/html"})
    )
    with pytest.raises(ValueError, match="not a text page"):
        await fetch_page("https://h.test/a.pdf")
    title, text = await fetch_page("https://h.test/latin")
    assert "café crème" in text and "�" not in text


def test_cross_check_samples_every_sub_topic():
    learnings = [Learning(f"{t}{i}", "S1", t) for t in ("a", "b", "c") for i in range(40)]  # 120 claims, 3 topics
    sample = spread_by_topic(learnings, 60)
    assert len(sample) == 60 and {x.sub_topic for x in sample} == {"a", "b", "c"}
    assert [x.text for x in sample[:3]] == ["a0", "b0", "c0"]
    assert spread_by_topic(learnings[:5], 60) == learnings[:5]


def test_the_generated_dirs_are_ignored_but_not_the_committed_files(tmp_path):
    k3 = tmp_path / ".k3code"
    k3.mkdir()
    ignore_generated(k3)
    body = (k3 / ".gitignore").read_text()
    assert all(f"{d}/\n" in body for d in GENERATED_DIRS) and "*" not in body and ".gitignore\n" in body
    (k3 / ".gitignore").write_text("*\n")  # what older versions wrote: it also hid rules/, skills/ and config.yaml
    ignore_generated(k3)
    assert "*" not in (k3 / ".gitignore").read_text()
    (k3 / ".gitignore").write_text("mine/\n")
    ignore_generated(k3)
    assert (k3 / ".gitignore").read_text() == "mine/\n"


async def test_a_null_claims_list_does_not_end_the_run(tmp_path, monkeypatch):
    steps = copy.deepcopy(STEPS)
    steps[1]["text"] = json.dumps({"claims": None, "new_questions": []})  # the extractor answered null for source a
    steps[2]["text"] = "this is not json at all"  # and prose for source b
    server = make(tmp_path, monkeypatch, steps, mode="auto", autonomy={"plan_first": False, "proposals": False})
    server.research_tools = FakeTools()
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await call(server, "slash.exec", {"command": "ultraresearch --n 2 What colour are the sky and grass?"})
    await server.session.turn_task
    report = server.session.stored.messages[-1]["content"]
    assert "Gamma Page" in report and "Report saved" in report  # the one source with claims is cited, the run finished
