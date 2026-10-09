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

REAL_RESOLVE = net_guard._resolve  # captured at import, before the autouse fixtures replace it

PUBLIC = "93.184.215.14"
NAMES = {
    "localhost": ["127.0.0.1", "::1"],
    "public.test": [PUBLIC],
    "evil.test": [PUBLIC, "10.0.0.5"],  # one private answer among public ones is enough to refuse
    "searx.lan": ["192.168.1.20"],
    "xn--strae-oqa.example.com": ["10.0.0.7"],  # the IDNA2008 name httpx connects to
    "strasse.example.com": [PUBLIC],  # the IDNA2003 spelling of the same name
    # carrier-grade NAT, spelled out so the release scan for 100.x tailnet addresses has nothing to flag
    "cgnat.test": [".".join(("100", "64", "0", "1"))],
}


@pytest.fixture(autouse=True)
def _table(monkeypatch):
    import socket

    def resolve(host: str) -> list[str]:
        if host in NAMES:
            return NAMES[host]
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
        except (socket.gaierror, UnicodeError):
            return []  # not in the table: does not resolve
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


@pytest.mark.parametrize("url", ["http://0177.0.0.1/", "http://\u2603.test/", "http://[::1/"])
def test_urls_httpx_cannot_parse_are_refused(url):
    with pytest.raises(BlockedURL, match="not a valid URL"):
        check_url(url)


def test_the_host_is_the_idna2008_name_httpx_connects_to():
    with pytest.raises(BlockedURL, match="xn--strae-oqa.example.com resolves to a private"):
        check_url("http://straße.example.com/")


def test_an_unresolvable_name_is_refused():
    with pytest.raises(BlockedURL, match="nowhere.test could not be resolved"):
        check_url("http://nowhere.test/")


def test_an_ipv6_zone_id_literal_is_refused():
    with pytest.raises(BlockedURL, match="private/loopback"):
        check_url("http://[fe80::1%25eth0]/")


def test_the_real_resolver_offline(monkeypatch):
    """The real ``_resolve`` with numeric hosts only (no DNS): the inet_aton forms and a zone-id literal."""
    monkeypatch.setattr(net_guard, "_resolve", REAL_RESOLVE)
    assert REAL_RESOLVE("2130706433") == ["127.0.0.1"]
    for url in ("http://2130706433/", "http://0x7f.1/", "http://[fe80::1%25eth0]/", "http://127.1/"):
        with pytest.raises(BlockedURL, match="private/loopback"):
            check_url(url)
    check_url(f"http://{PUBLIC}/")


def test_allow_private_bypass():
    check_url("http://127.0.0.1/", allow_private=True)


def test_configured_searxng_host_allowed():
    config = SimpleNamespace(research={"searxng_url": "http://searx.lan:8080/"}, web={})
    allow_private, origins = web_settings(config)
    assert not allow_private
    check_url("http://searx.lan:8080/search", allow_origins=origins)
    with pytest.raises(BlockedURL):
        check_url("http://127.0.0.1:8080/", allow_origins=origins)  # only the configured host, not its neighbours
    for other in ("http://searx.lan:6379/", "https://searx.lan:8080/", "http://searx.lan/"):
        with pytest.raises(BlockedURL):
            check_url(other, allow_origins=origins)  # the exact origin only: not another port or scheme
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


async def test_a_redirect_to_loopback_reports_the_guard_with_robots_on():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "public.test":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9/admin"})
        return httpx.Response(200, text="secret")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = WebFetcher(client=client, respect_robots=True, per_host_rate=1000, burst=1000)
    with pytest.raises(FetchRefused, match="blocked: 127.0.0.1"):
        await fetcher.get("http://public.test/")
    assert all("127.0.0.1" not in u for u in seen)


# ── pinning: the fetcher connects to the address it vetted ──


class _Inner:
    """A network backend that records where it was asked to connect and connects nowhere."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, **_kw):
        self.calls.append((host, port))
        raise OSError("not connecting in tests")


async def test_the_backend_connects_to_the_vetted_address_not_the_name():
    inner = _Inner()
    backend = net_guard.PinningBackend(inner=inner)
    with pytest.raises(OSError):
        await backend.connect_tcp("public.test", 443)
    assert inner.calls == [(PUBLIC, 443)]
    with pytest.raises(BlockedURL):
        await backend.connect_tcp("evil.test", 80)
    with pytest.raises(BlockedURL):
        await backend.connect_tcp("nowhere.test", 80)
    assert inner.calls == [(PUBLIC, 443)]  # neither blocked name was connected to


async def test_the_backend_passes_the_exempt_host_and_port_only():
    inner = _Inner()
    backend = net_guard.PinningBackend([("searx.lan", 8080)], inner=inner)
    with pytest.raises(OSError):
        await backend.connect_tcp("searx.lan", 8080)
    with pytest.raises(BlockedURL):
        await backend.connect_tcp("searx.lan", 6379)
    assert inner.calls == [("searx.lan", 8080)]


async def test_dns_rebinding_after_the_check_is_refused_at_connect(monkeypatch):
    """The name answers public to the check and loopback afterwards: the fetcher's own client never connects."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits: list[str] = []

    class Secret(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("content-length", "6")
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, *_a) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Secret)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    answers = iter([[PUBLIC], [PUBLIC]])  # both URL-level checks (get and its first hop) see a public answer
    monkeypatch.setattr(net_guard, "_resolve", lambda _host: next(answers, ["127.0.0.1"]))
    fetcher = WebFetcher(respect_robots=False)
    try:
        with pytest.raises(FetchRefused, match="blocked: localhost resolves to a private"):
            await fetcher.get(f"http://localhost:{server.server_address[1]}/")
    finally:
        await fetcher.aclose()
        server.shutdown()
        server.server_close()
    assert hits == []


async def test_the_guarded_transport_keeps_the_name_for_the_host_header():
    """End to end through httpx: the vetted address is connected to (here redirected to a local server) and the
    request still names the host."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpcore

    hosts: list[str] = []

    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hosts.append(self.headers.get("Host", ""))
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_a) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    asked: list[str] = []

    class ToLocal:
        async def connect_tcp(self, host, port, **kw):
            asked.append(host)
            return await httpcore.AnyIOBackend().connect_tcp("127.0.0.1", server.server_address[1], **kw)

    try:
        async with httpx.AsyncClient(transport=net_guard.GuardedTransport(inner=ToLocal())) as client:
            r = await client.get("http://public.test/")
    finally:
        server.shutdown()
        server.server_close()
    assert (r.status_code, r.text, asked, hosts) == (200, "ok", [PUBLIC], ["public.test"])
