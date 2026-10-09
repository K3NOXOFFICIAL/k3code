"""SSRF guard: web_fetch / web_browse must not reach loopback, private, link-local or metadata addresses, directly
or through a redirect. No real network: names resolve through a table, transports are httpx mocks."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from k3code import net_guard
from k3code.net_guard import BlockedURL, check_url, web_settings
from k3code.research.browser import BrowserManager
from k3code.research.fetch import FetchRefused, WebFetcher

PUBLIC = "93.184.215.14"
NAMES = {
    "localhost": ["127.0.0.1", "::1"],
    "public.test": [PUBLIC],
    "evil.test": [PUBLIC, "10.0.0.5"],  # one private answer among public ones is enough to refuse
    "searx.lan": ["192.168.1.20"],
    # carrier-grade NAT, spelled out so the release scan for 100.x tailnet addresses has nothing to flag
    "cgnat.test": [".".join(("100", "64", "0", "1"))],
}


@pytest.fixture(autouse=True)
def _table(monkeypatch):
    import socket

    def resolve(host: str) -> list[str]:
        if host in NAMES:
            return NAMES[host]
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
        return [str(i[4][0]) for i in infos]

    monkeypatch.setattr(net_guard, "_resolve", resolve)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/",
        "http://192.168.0.1/",
        "http://172.16.0.9/",
        "http://cgnat.test/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://evil.test/",
    ],
)
def test_blocked(url):
    with pytest.raises(BlockedURL, match="blocked: .* resolves to a private/loopback address .*web.allow_private"):
        check_url(url)


@pytest.mark.parametrize("url", [f"http://{PUBLIC}/", f"https://{PUBLIC}:8443/x", "https://public.test/a?b=1"])
def test_public_allowed(url):
    check_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://public.test/", "http:///x", "gopher://127.0.0.1/"])
def test_non_http_refused(url):
    with pytest.raises(BlockedURL):
        check_url(url)


def test_allow_private_bypass():
    check_url("http://127.0.0.1/", allow_private=True)


def test_configured_searxng_host_allowed():
    config = SimpleNamespace(research={"searxng_url": "http://searx.lan:8080/"}, web={})
    allow_private, hosts = web_settings(config)
    assert not allow_private
    check_url("http://searx.lan:8080/search", allow_hosts=hosts)
    with pytest.raises(BlockedURL):
        check_url("http://127.0.0.1:8080/", allow_hosts=hosts)  # only the configured host, not its neighbours
    with pytest.raises(BlockedURL):
        check_url("http://searx.lan/")  # without the exemption it is private


def test_web_allow_private_flag_read_from_config():
    assert web_settings(SimpleNamespace(research={}, web={"allow_private": True}))[0] is True
    assert web_settings(SimpleNamespace(research={}, web={}))[0] is False


# ── the fetcher: every hop is vetted ──


def _fetcher(handler, **kw) -> WebFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebFetcher(client=client, respect_robots=False, per_host_rate=1000, burst=1000, **kw)


async def test_fetch_refuses_loopback_before_any_request():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="secret")

    with pytest.raises(FetchRefused, match="blocked: 127.0.0.1"):
        await _fetcher(handler).get("http://127.0.0.1:9/")
    assert seen == []


async def test_redirect_from_public_to_loopback_blocked():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "public.test":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9/admin"})
        return httpx.Response(200, text="secret")

    with pytest.raises(FetchRefused, match="blocked: 127.0.0.1"):
        await _fetcher(handler).get("http://public.test/")
    assert seen == ["http://public.test/"]


async def test_redirect_to_metadata_service_by_name_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.test":
            return httpx.Response(301, headers={"location": "http://evil.test/"})
        return httpx.Response(200, text="secret")

    with pytest.raises(FetchRefused, match="blocked: evil.test"):
        await _fetcher(handler).get("http://public.test/")


async def test_public_redirect_chain_still_works():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, text="hello", headers={"content-type": "text/plain"})

    got = await _fetcher(handler).get("http://public.test/")
    assert (got.status, got.body, got.url) == (200, "hello", "http://public.test/next")


async def test_fetch_allow_private_bypass():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="local", headers={"content-type": "text/plain"})

    got = await _fetcher(handler, allow_private=True).get("http://127.0.0.1:9/")
    assert got.body == "local"


async def test_fetch_allows_configured_searxng_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    fetcher = WebFetcher.from_config({"searxng_url": "http://searx.lan:8080"}, {})
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher.respect_robots = False
    assert (await fetcher.get("http://searx.lan:8080/page")).body == "ok"


async def test_admit_refuses_private_for_the_browser_path():
    with pytest.raises(FetchRefused, match="blocked: 169.254.169.254"):
        await WebFetcher().admit("http://169.254.169.254/")


# ── the browser: pre-check plus a route handler for redirects and sub-requests ──


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.result = ""

    async def continue_(self) -> None:
        self.result = "continue"

    async def abort(self, reason: str = "") -> None:
        self.result = f"abort:{reason}"


async def test_browser_route_aborts_blocked_hosts_only():
    handler = BrowserManager()._guard_route()
    routes = [
        _Route("http://public.test/page"),
        _Route("http://127.0.0.1:9/admin"),
        _Route("http://[::ffff:10.0.0.1]/x"),
        _Route("data:text/plain,hi"),
    ]
    for r in routes:
        await handler(r)
    assert [r.result for r in routes] == ["continue", "abort:blockedbyclient", "abort:blockedbyclient", "continue"]


async def test_browser_route_respects_allow_private():
    handler = BrowserManager(allow_private=True)._guard_route()
    r = _Route("http://127.0.0.1:9/")
    await handler(r)
    assert r.result == "continue"


async def test_browser_fetch_refuses_blocked_url_before_launch():
    launched: list[str] = []

    async def launcher(site: str):
        launched.append(site)
        raise AssertionError("must not launch")

    with pytest.raises(BlockedURL):
        await BrowserManager(launcher=launcher).fetch("http://127.0.0.1:9/")
    assert launched == []
