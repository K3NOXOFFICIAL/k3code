"""What web_fetch and web_search say when the machine has no network: a name that does not resolve is not a policy
block, and a search whose every backend failed to connect says the network is down (not that the setup is wrong)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from k3code import net_guard
from k3code.net_guard import BlockedURL, check_url
from k3code.research.tools import SearxngSearch, register_web_tools

DDG = "https://html.duckduckgo.com/html/"
SEARX = "http://searx.test"
UNREACHABLE = "web_search failed: network unreachable (no backend answered); do not retry"


class _Registry:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register(self, spec, fn) -> None:
        self.handlers[spec.name] = fn


class _Mcp:
    """A connected MCP search tool that answers every call with ``reply``."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.tool = SimpleNamespace(
            name="hub_searxng__search",
            qualified="mcp__example__hub_searxng__search",
            schema={"properties": {"query": {}}},
        )

    def tools(self):
        return [self.tool]

    async def call(self, qualified, args):
        return self.reply


async def _search(research: dict[str, Any], mcp: Any = None) -> dict[str, Any]:
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research=research), mcp=mcp)
    return await reg.handlers["web_search"]({"query": "python asyncio tutorial"})


# ── web_fetch: a name that does not resolve ──


@pytest.fixture
def resolves(monkeypatch):
    def set_answers(answers: list[str]) -> None:
        monkeypatch.setattr(net_guard, "_resolve", lambda host: answers)

    return set_answers


def test_a_name_that_does_not_resolve_says_so_and_is_not_called_blocked(resolves):
    resolves([])
    with pytest.raises(BlockedURL) as err:
        check_url("http://example.com/")
    assert str(err.value) == "could not resolve example.com (DNS failed: no network or no such host)"
    assert "blocked" not in str(err.value)


def test_a_policy_block_still_says_blocked(resolves):
    resolves(["10.0.0.5"])
    with pytest.raises(BlockedURL, match=r"^blocked: example\.com resolves to a private/loopback address"):
        check_url("http://example.com/")
    with pytest.raises(BlockedURL, match=r"^blocked: not an http\(s\) URL"):
        check_url("ftp://example.com/")
    with pytest.raises(BlockedURL, match=r"^blocked: 127\.0\.0\.1 resolves to a private"):
        check_url("http://127.0.0.1/")


async def test_web_fetch_reports_the_failed_lookup_as_a_failed_lookup(resolves):
    resolves([])
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}, web={}))
    out = await reg.handlers["web_fetch"]({"url": "https://example.com"})
    assert out == {"error": "web_fetch failed: could not resolve example.com (DNS failed: no network or no such host)"}
    resolves(["10.0.0.5"])
    out = await reg.handlers["web_fetch"]({"url": "https://example.com"})
    assert out["error"].startswith("web_fetch failed: blocked: example.com resolves to a private/loopback address")


# ── web_search: every backend failed to connect ──


@respx.mock
async def test_no_network_says_so_instead_of_listing_unset_backends():
    respx.post(DDG).mock(side_effect=httpx.ConnectError("Network is unreachable"))
    out = await _search({})
    assert out == {"error": UNREACHABLE}


@respx.mock
async def test_a_connect_timeout_is_no_network_too():
    respx.post(DDG).mock(side_effect=httpx.ConnectTimeout("timed out"))
    assert await _search({}) == {"error": UNREACHABLE}


@respx.mock
async def test_a_configured_searxng_that_cannot_be_reached_is_no_network_too():
    respx.get(f"{SEARX}/search").mock(side_effect=httpx.ConnectError("refused"))
    respx.post(DDG).mock(side_effect=httpx.ConnectError("Network is unreachable"))
    assert await _search({"searxng_url": SEARX}) == {"error": UNREACHABLE}


@respx.mock
async def test_a_backend_that_answered_badly_keeps_the_full_account_and_its_advice():
    # SearXNG is up but errors: the network is fine, so the setup advice stays
    respx.get(f"{SEARX}/search").mock(return_value=httpx.Response(500))
    respx.post(DDG).mock(side_effect=httpx.ConnectError("Network is unreachable"))
    out = await _search({"searxng_url": SEARX})
    assert out["error"].startswith("web_search is disabled:")
    assert "set research.searxng_url to a working instance" in out["error"] and "DuckDuckGo failed" in out["error"]


@respx.mock
async def test_duckduckgo_answering_with_an_http_error_is_not_no_network():
    respx.post(DDG).mock(return_value=httpx.Response(503))
    out = await _search({})
    assert out["error"].startswith("web_search is disabled:") and "DuckDuckGo failed: HTTPStatusError" in out["error"]


@respx.mock
async def test_a_failing_mcp_tool_beside_a_dead_network_keeps_the_full_account():
    respx.post(DDG).mock(side_effect=httpx.ConnectError("Network is unreachable"))
    out = await _search({}, mcp=_Mcp({"error": "tool crashed"}))
    assert out["error"].startswith("web_search is disabled:") and "tool crashed" in out["error"]


@respx.mock
async def test_nothing_set_up_keeps_the_advice_to_configure_a_backend():
    out = await _search({"keyless_fallback": False})
    assert out["error"].startswith("web_search is disabled:")
    assert "no SearXNG URL configured (set research.searxng_url)" in out["error"]
    assert (
        "keyless DuckDuckGo fallback is off" in out["error"] and "no MCP web search tool is connected" in out["error"]
    )


@respx.mock
async def test_a_working_backend_after_a_dead_one_still_answers():
    respx.post(DDG).mock(side_effect=httpx.ConnectError("Network is unreachable"))
    mcp = _Mcp({"content": '{"results": [{"title": "H", "url": "http://h.test", "content": ""}]}'})
    out = await _search({}, mcp=mcp)
    assert "http://h.test" in out["content"]


@respx.mock
async def test_searxng_remembers_why_its_probe_failed():
    respx.get(f"{SEARX}/search").mock(side_effect=httpx.ConnectError("refused"))
    down = SearxngSearch(SEARX)
    assert not await down.available() and down.unreachable
    respx.get(f"{SEARX}/search").mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    odd = SearxngSearch(SEARX)
    assert not await odd.available() and not odd.unreachable
    assert not await SearxngSearch(None).available()
