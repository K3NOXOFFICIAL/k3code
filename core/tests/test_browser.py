"""The browser tool: classification, escalation from fetch, the stop rules, profiles and the environment.

The stop rules are checked with a fake browser whose page refuses every action except navigation, so a CAPTCHA,
login or paywall can only end the read. The real-Chromium tests run against a local HTTP server and are skipped,
with the reason printed, when Playwright or its Chromium is not installed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from k3code.research import browser as b
from k3code.research.browser import BrowserManager, BrowserUnavailable, Stopped, Verdict, classify
from k3code.research.fetch import FetchStatus, WebFetcher
from k3code.research.tools import fetch_page, register_web_tools

ARTICLE = "<html><head><title>Real Title</title></head><body><main>" + ("word " * 400) + "</main></body></html>"
CHALLENGE = (
    "<html><head><title>Attention Required! | Cloudflare</title></head><body>"
    "<h1>Attention Required! | Cloudflare</h1></body></html>"
)
CAPTCHA = '<html><body><form><div class="g-recaptcha" data-sitekey="x"></div></form></body></html>'
PAYWALL = (
    "<html><body><main>"
    + ("teaser words " * 120)
    + "<p>Subscribe to continue reading this article.</p></main></body></html>"
)
LOGIN = "<html><body><h1>Please log in to view this page</h1><input type=password></body></html>"


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _Registry:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def register(self, spec, fn) -> None:
        self.handlers[spec.name] = fn


class ActionRefused(AssertionError):
    pass


class FakePage:
    """Navigation and reading only. Any other action (click, fill, type, submit, evaluate) fails the test."""

    def __init__(self, pages: dict[str, tuple[int, str]], log: list) -> None:
        self._pages, self.log = pages, log
        self.url = ""
        self._html = ""

    async def goto(self, url: str, **_kw):
        self.log.append(("goto", url))
        status, self._html = self._pages.get(url, (404, "not found"))
        self.url = url
        return SimpleNamespace(status=status)

    async def content(self) -> str:
        return self._html

    async def title(self) -> str:
        return ""

    def on(self, _event: str, _handler) -> None:  # downloads are not part of these fixtures
        return None

    async def close(self) -> None:
        self.log.append(("close",))

    def __getattr__(self, name: str):
        raise ActionRefused(f"the browser attempted {name!r}: only navigation is allowed")


class FakeContext:
    def __init__(self, pages: dict, log: list) -> None:
        self._pages, self.log = pages, log
        self.guards: list = []  # route / route_web_socket registrations (the SSRF guard), not page actions
        self.closed = False

    async def new_page(self) -> FakePage:
        return FakePage(self._pages, self.log)

    async def route(self, pattern: str, handler) -> None:  # the SSRF guard's request filter; see test_net_guard
        self.guards.append(("route", pattern, handler))

    async def route_web_socket(self, pattern, handler) -> None:
        self.guards.append(("route_web_socket", pattern, handler))

    async def close(self) -> None:
        self.closed = True
        self.log.append(("context-close",))


def fake_browser(pages: dict[str, tuple[int, str]]) -> tuple[BrowserManager, list]:
    log: list = []

    async def launcher(site: str):
        log.append(("launch", site))
        return FakeContext(pages, log)

    return BrowserManager(challenge_wait=0, launcher=launcher), log


def fetcher(**kw) -> WebFetcher:
    kw.setdefault("respect_robots", False)
    return WebFetcher(**kw)


# ── classification: pure, no browser ──


@pytest.mark.parametrize(
    ("status", "body", "verdict"),
    [
        (200, ARTICLE, Verdict.OK),
        (403, "Forbidden", Verdict.BLOCKED),
        (200, CHALLENGE, Verdict.BLOCKED),
        (503, "<title>Just a moment...</title>", Verdict.BLOCKED),
        (200, CAPTCHA, Verdict.CAPTCHA),
        (200, PAYWALL, Verdict.PAYWALL),
        (402, "", Verdict.PAYWALL),
        (200, LOGIN, Verdict.LOGIN),
        (401, "", Verdict.LOGIN),
        (500, "boom", Verdict.ERROR),
        (0, "", Verdict.ERROR),
    ],
)
def test_classify_sorts_answers_into_read_block_or_stop(status, body, verdict):
    assert classify(status, body) is verdict


def test_a_long_page_with_a_captcha_widget_is_a_form_not_a_gate():
    long_form = "<html><body><main>" + ("article text " * 200) + CAPTCHA + "</main></body></html>"
    assert classify(200, long_form) is Verdict.OK


def test_profiles_live_under_the_k3code_home_and_never_under_home(tmp_path, monkeypatch):
    k3home = tmp_path / "k3home"
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    path = b.profile_dir("Example.COM")
    assert path == k3home.resolve() / "browser" / "profiles" / "example.com"
    assert path.is_relative_to(k3home.resolve()) and not path.is_relative_to(tmp_path / "userhome")
    traversal = b.profile_dir("../../../etc/passwd")
    assert traversal.is_relative_to(k3home.resolve())


def test_the_browser_environment_carries_no_keys_and_its_home_is_inside_k3code(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3"))
    monkeypatch.setenv("OMNIROUTE_API_KEY", "must-not-leak")
    monkeypatch.setenv("K3CODE_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    env = b.scrubbed_env()
    assert set(env) <= {"PATH", "HOME", "LANG"}
    assert "must-not-leak" not in env.values()
    assert env["HOME"].startswith(str(tmp_path / "k3"))


def test_the_grep_guard_no_captcha_solver_login_automation_or_stealth_module_is_imported():
    """CAPTCHA solving, login automation, paywall bypass, stealth spoofing and residential proxies are out of scope."""
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:twocaptcha|2captcha|anticaptcha|capsolver|capmonster|python_anticaptcha|"
        r"playwright_stealth|undetected_chromedriver|selenium_stealth|cloudscraper|tls_client|curl_cffi|"
        r"puppeteer_extra|solver)\b",
        re.MULTILINE,
    )
    src = Path(b.__file__).resolve().parents[1]  # core/src/k3code
    hits = [str(p) for p in src.rglob("*.py") if forbidden.search(p.read_text(encoding="utf-8"))]
    assert hits == []
    # no definition of a solver, a stealth layer, a paywall bypass or a proxy pool anywhere in the package
    defs = re.compile(
        r"^\s*(?:def|class)\s+_*(?:solve|stealth|spoof|bypass|residential|proxy_?pool)\w*"
        r"|^\s*(?:def|class)\s+\w*(?:captcha|solver)\w*",
        re.I | re.MULTILINE,
    )
    assert not [str(p) for p in src.rglob("*.py") if defs.search(p.read_text(encoding="utf-8"))]


# ── escalation and the stop rules, with a fake browser ──


@respx.mock
async def test_a_403_escalates_to_the_browser_and_reads_what_renders():
    respx.get("https://blocked.test/article").mock(return_value=httpx.Response(403, text=CHALLENGE))
    browser, log = fake_browser({"https://blocked.test/article": (200, ARTICLE)})
    title, text = await fetch_page("https://blocked.test/article", fetcher=fetcher(), browser=browser)
    assert title == "Real Title" and "word word" in text
    assert [e for e in log if e[0] == "goto"] == [("goto", "https://blocked.test/article")]  # one navigation only


@respx.mock
async def test_a_page_read_in_the_browser_keeps_its_links_absolute():
    """Issue #99: the browser path resolves relative links against the rendered page's URL too."""
    respx.get("https://blocked.test/docs/").mock(return_value=httpx.Response(403, text=CHALLENGE))
    page = '<html><body><main><p>Next: <a href="step2.html">step 2</a></p></main></body></html>'
    browser, _log = fake_browser({"https://blocked.test/docs/": (200, page)})
    _, text = await fetch_page("https://blocked.test/docs/", fetcher=fetcher(), browser=browser)
    assert text == "Next: [step 2](https://blocked.test/docs/step2.html)"
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}), fetcher=fetcher(), browser=browser)
    out = await reg.handlers["web_browse"]({"url": "https://blocked.test/docs/"})
    assert out["content"].endswith("Next: [step 2](https://blocked.test/docs/step2.html)")


@respx.mock
async def test_a_403_without_a_browser_is_reported_not_retried():
    respx.get("https://blocked.test/x").mock(return_value=httpx.Response(403, text=CHALLENGE))
    with pytest.raises(FetchStatus):
        await fetch_page("https://blocked.test/x", fetcher=fetcher(), browser=None)


@respx.mock
@pytest.mark.parametrize(
    ("status", "body", "word"),
    [(200, CAPTCHA, "CAPTCHA"), (200, PAYWALL, "paywall"), (200, LOGIN, "login wall")],
)
async def test_captcha_login_and_paywall_stop_with_a_report_and_no_action(status, body, word):
    url = "https://gated.test/page"
    respx.get(url).mock(return_value=httpx.Response(status, text=body))
    browser, log = fake_browser({url: (status, body)})
    with pytest.raises(Stopped, match=word):
        await fetch_page(url, fetcher=fetcher(), browser=browser)
    # the answer itself is the stop: the browser is not even started for it
    assert not [e for e in log if e[0] in {"launch", "goto"}]


@respx.mock
async def test_a_captcha_met_in_the_browser_stops_the_read_and_is_never_solved():
    url = "https://wall.test/page"
    respx.get(url).mock(return_value=httpx.Response(403, text=CHALLENGE))
    browser, log = fake_browser({url: (200, CAPTCHA)})
    with pytest.raises(Stopped, match="does not solve CAPTCHAs"):
        await fetch_page(url, fetcher=fetcher(), browser=browser)
    assert not [e for e in log if e[0] not in {"goto", "close", "launch", "context-close"}]


@respx.mock
async def test_the_agent_browse_tool_reports_stops_and_missing_playwright(monkeypatch):
    for host in ("gated.test", "ok.test"):
        respx.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))
    url = "https://gated.test/a"
    browser, _log = fake_browser({url: (200, CAPTCHA), "https://ok.test/a": (200, ARTICLE)})
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}), browser=browser)
    out = await reg.handlers["web_browse"]({"url": url})
    assert "error" in out and "CAPTCHA" in out["error"]
    ok = await reg.handlers["web_browse"]({"url": "https://ok.test/a"})
    assert "Real Title" in ok["content"]

    def missing():
        raise BrowserUnavailable(b.INSTALL_HINT)

    import importlib.util

    monkeypatch.setattr(b, "playwright_module", missing)
    real_find_spec = importlib.util.find_spec
    # the package is importable (so the tool is offered) but the import itself fails: the call says why
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: object() if name == "playwright" else real_find_spec(name, *a)
    )
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}), browser=BrowserManager(challenge_wait=0))
    out = await reg.handlers["web_browse"]({"url": "https://ok.test/a"})
    assert "unavailable" in out["error"] and "playwright" in out["error"].lower()
    # no Playwright installed: the tool is not offered at all (its schema would cost tokens on every request)
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "playwright" else real_find_spec(name, *a)
    )
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}), browser=BrowserManager(challenge_wait=0))
    assert "web_browse" not in reg.handlers and "web_fetch" in reg.handlers


async def test_the_agent_browse_tool_leaves_room_for_its_header():
    """A long URL above the page put the result over what the model is sent: the loop cut its middle."""
    from k3code.providers.types import Message
    from k3code.tools import MAX_TOOL_RESULT_CHARS, clip_tool_results

    url = "https://ok.test/a?utm=" + "x" * 680
    words = " ".join(f"w{i:05d}" for i in range(6_000))
    page = f"<html><head><title>Real Title</title></head><body><main><p>{words}</p></main></body></html>"
    browser, _log = fake_browser({url: (200, page)})
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}), fetcher=fetcher(), browser=browser)
    content = (await reg.handlers["web_browse"]({"url": url}))["content"]
    assert "continue with start_index=" in content
    sent = clip_tool_results([Message(role="tool", name="web_browse", content=content, tool_call_id="1")])
    assert sent[0].content == content and len(content) <= MAX_TOOL_RESULT_CHARS


@respx.mock
async def test_fetch_still_works_without_playwright_and_says_so_for_a_blocked_page(monkeypatch):
    def missing():
        raise BrowserUnavailable(b.INSTALL_HINT)

    monkeypatch.setattr(b, "playwright_module", missing)
    for host in ("plain.test", "walled.test"):
        respx.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://plain.test/a").mock(return_value=httpx.Response(200, text=ARTICLE))
    respx.get("https://walled.test/a").mock(return_value=httpx.Response(403, text=CHALLENGE))
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}), browser=BrowserManager(challenge_wait=0))
    assert "Real Title" in (await reg.handlers["web_fetch"]({"url": "https://plain.test/a"}))["content"]
    out = await reg.handlers["web_fetch"]({"url": "https://walled.test/a"})
    assert "Playwright" in out["error"]


# ── real Chromium against a local server (skipped without Playwright or Chromium) ──


class _Site(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, str]] = []
    page_override: str | None = None  # the body served at /guarded

    def do_GET(self):  # noqa: N802
        ua = self.headers.get("User-Agent", "")
        self.requests.append(("GET", self.path, ua))
        if self.path == "/gate" and "k3code-research" in ua:
            body, status = CHALLENGE.encode(), 403  # plain HTTP is refused, a real browser is not
        elif self.path == "/captcha":
            body, status = CAPTCHA.encode(), 200
        elif self.path == "/download":
            body, status = b"quarterly figures\n", 200
            self.send_response(status)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("content-disposition", 'attachment; filename="report.txt"')
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path == "/guarded" and self.page_override:
            body, status = self.page_override.encode(), 200
        else:
            body, status = ARTICLE.encode(), 200
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self.requests.append(("POST", self.path, self.headers.get("User-Agent", "")))
        self.send_response(405)
        self.end_headers()

    def log_message(self, *_args) -> None:
        return None


@pytest.fixture
def local_site():
    _Site.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _Site.requests
    server.shutdown()
    server.server_close()


def _playwright_or_skip() -> None:
    if importlib.util.find_spec("playwright") is None:
        pytest.skip("Playwright is not installed (optional browser extra): real-browser tests skipped")


async def _launch_or_skip(manager: BrowserManager, url: str):
    try:
        return await manager.fetch(url)
    except BrowserUnavailable as e:
        pytest.skip(f"Chromium for Playwright is not installed: real-browser tests skipped ({str(e)[:80]})")
    except Exception as e:  # noqa: BLE001 - a missing browser binary surfaces as a launch error
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e).lower():
            pytest.skip("Chromium for Playwright is not installed: real-browser tests skipped")
        raise


async def test_real_browser_escalation_reads_the_page_a_plain_fetch_was_refused(tmp_path, monkeypatch, local_site):
    _playwright_or_skip()
    base, requests = local_site
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3"))
    manager = BrowserManager(challenge_wait=0, allow_private=True)  # the local site is on loopback
    try:
        await _launch_or_skip(manager, f"{base}/article")
        title, text = await fetch_page(f"{base}/gate", fetcher=fetcher(allow_private=True), browser=manager)
    finally:
        await manager.close()
    assert title == "Real Title" and "word word" in text
    assert ("GET", "/gate", next(ua for _m, p, ua in requests if p == "/gate")) in requests
    assert (tmp_path / "k3" / "browser" / "profiles" / "127.0.0.1").is_dir()  # the profile lives under k3code


async def test_real_browser_stops_at_a_captcha_and_posts_nothing(tmp_path, monkeypatch, local_site):
    _playwright_or_skip()
    base, requests = local_site
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3"))
    manager = BrowserManager(challenge_wait=0, allow_private=True)  # the local site is on loopback
    try:
        await _launch_or_skip(manager, f"{base}/article")
        with pytest.raises(Stopped, match="CAPTCHA"):
            await fetch_page(f"{base}/captcha", fetcher=fetcher(allow_private=True), browser=manager)
    finally:
        await manager.close()
    assert requests and all(method == "GET" for method, _p, _ua in requests)


async def test_real_browser_saves_a_download_under_the_k3code_home_only(tmp_path, monkeypatch, local_site):
    _playwright_or_skip()
    base, _requests = local_site
    k3home = tmp_path / "k3"
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    manager = BrowserManager(challenge_wait=0, allow_private=True)  # the local site is on loopback
    try:
        page = await _launch_or_skip(manager, f"{base}/article")
        assert page.status == 200
        got = await manager.fetch(f"{base}/download")
    finally:
        await manager.close()
    assert len(got.downloads) == 1
    saved = Path(got.downloads[0])
    assert saved.is_relative_to(k3home.resolve()) and saved.read_text() == "quarterly figures\n"


@respx.mock
async def test_web_browse_refuses_a_robots_disallowed_page_before_the_browser_starts():
    """robots.txt is honoured by the browser tool too: a disallowed page never reaches the browser."""
    respx.get("https://rules.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /"))
    browser, log = fake_browser({"https://rules.test/private": (200, ARTICLE)})
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}), browser=browser)
    out = await reg.handlers["web_browse"]({"url": "https://rules.test/private"})
    assert "robots.txt disallows" in out["error"]
    assert log == []  # no launch, no navigation


async def test_an_escalated_page_takes_a_token_of_the_hosts_rate_budget(monkeypatch):
    """The browser's own navigation to the same host is spaced like any request: one token, one second here."""
    clock = FakeClock()
    url = "https://budget.test/article"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(403, text=CHALLENGE))
        browser, _log = fake_browser({url: (200, ARTICLE)})
        web = fetcher(now=clock.now, sleep=clock.sleep, per_host_rate=1.0, burst=1.0)
        await fetch_page(url, fetcher=web, browser=browser)  # the HTTP fetch takes the only token
    assert clock.t == 1.0  # the browser's navigation waited for the next token


async def test_a_huge_dom_is_capped_and_parsed_off_the_event_loop(monkeypatch):
    import threading

    from k3code.research import tools as t

    monkeypatch.setattr(b, "MAX_SNAPSHOT_CHARS", 1000)
    big = "<html><body><main><p>" + "word " * 5000 + "</p></main></body></html>"
    page = FakePage({"https://big.test/": (200, big)}, [])
    rendered = await b.BrowserManager._read(SimpleNamespace(challenge_wait=0), page, "https://big.test/", [], [])
    assert rendered.truncated and len(rendered.html) == 1000

    seen: list[str] = []
    real = t.extract_text
    monkeypatch.setattr(t, "extract_text", lambda m, *a: (seen.append(threading.current_thread().name), real(m, *a))[1])

    class Browser:
        async def fetch(self, url):
            return rendered

    class Fetcher:
        async def admit(self, url):
            return None

    _, text = await t._read_rendered(Browser(), "https://big.test/", Fetcher())
    assert seen and seen[0] != threading.main_thread().name
    assert "[page cut" in text


# ── the SSRF guard inside Chromium: routes on the context, service workers blocked ──


async def test_fetch_routes_every_request_and_websocket_of_the_context_through_the_guard():
    browser, log = fake_browser({"https://public.test/a": (200, ARTICLE)})
    await browser.fetch("https://public.test/a")
    routes = browser._contexts["public.test"].guards
    assert [e[0] for e in routes] == ["route", "route_web_socket"]  # once, on the context
    assert routes[0][1] == "**/*"
    seen: list[str] = []

    async def abort(reason: str = "") -> None:
        seen.append(reason)

    blocked = SimpleNamespace(request=SimpleNamespace(url="http://127.0.0.1:9/admin"), abort=abort)
    await routes[0][2](blocked)  # the attached handler is the guard
    assert seen == ["blockedbyclient"]
    await browser.fetch("https://public.test/a")
    assert len(routes) == 2 and ("launch", "public.test") in log  # the context is reused, its routes are not stacked


async def test_launch_blocks_service_workers(monkeypatch, tmp_path):
    seen: dict = {}

    class _Chromium:
        async def launch_persistent_context(self, _profile, **opts):
            seen.update(opts)
            return SimpleNamespace()

    class _PW:
        async def start(self):
            return SimpleNamespace(chromium=_Chromium())

    monkeypatch.setattr(b, "playwright_module", lambda: SimpleNamespace(async_playwright=lambda: _PW()))
    await BrowserManager(home=tmp_path)._launch_playwright("public.test")
    assert seen["service_workers"] == "block"


# ── one Chromium per site, however many reads race for it, and none kept forever ──


class SlowPage(FakePage):
    """Takes a while to load, and fails like Playwright if its context is closed while it loads."""

    def __init__(self, ctx: FakeContext, pages: dict, log: list) -> None:
        super().__init__(pages, log)
        self.ctx = ctx

    async def goto(self, url: str, **kw):
        await asyncio.sleep(0.3)
        if self.ctx.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return await super().goto(url, **kw)


def slow_browser(
    pages: dict, *, slow_sites: frozenset = frozenset(), slow_close: frozenset = frozenset(), **kw
) -> tuple[BrowserManager, list, dict]:
    """A launcher that takes a while to start, like Chromium; ``made`` holds every context it launched, per site.
    Pages of ``slow_sites`` load slowly; contexts of ``slow_close`` take a while to close."""
    log: list = []
    made: dict[str, list[FakeContext]] = {}

    async def launcher(site: str):
        log.append(("launch", site))
        await asyncio.sleep(0.05)
        ctx = FakeContext(pages, log)
        if site in slow_sites:
            ctx.new_page = lambda: _ready(SlowPage(ctx, pages, log))
        if site in slow_close:

            async def close() -> None:
                log.append(("close-start", site))
                await asyncio.sleep(0.1)
                ctx.closed = True
                log.append(("close-done", site))

            ctx.close = close
        made.setdefault(site, []).append(ctx)
        return ctx

    return BrowserManager(challenge_wait=0, launcher=launcher, **kw), log, made


async def _ready(value):
    return value


async def test_concurrent_reads_of_one_site_launch_one_browser():
    pages = {f"https://docs.test/p{i}": (200, ARTICLE) for i in range(3)}
    browser, log, made = slow_browser(pages)
    got = await asyncio.gather(*(browser.fetch(f"https://docs.test/p{i}") for i in range(3)))
    assert [p.status for p in got] == [200, 200, 200]
    assert [e for e in log if e[0] == "launch"] == [("launch", "docs.test")]
    assert len(made["docs.test"]) == 1 and len(made["docs.test"][0].guards) == 2


async def test_concurrent_launches_start_one_playwright_driver(monkeypatch, tmp_path):
    starts: list = []

    class _Chromium:
        async def launch_persistent_context(self, _profile, **_opts):
            return SimpleNamespace()

    class _PW:
        async def start(self):
            starts.append(1)
            await asyncio.sleep(0.05)
            return SimpleNamespace(chromium=_Chromium(), stop=lambda: _ready(None))

    monkeypatch.setattr(b, "playwright_module", lambda: SimpleNamespace(async_playwright=lambda: _PW()))
    browser = BrowserManager(home=tmp_path)
    await asyncio.gather(browser._launch_playwright("a.test"), browser._launch_playwright("b.test"))
    assert len(starts) == 1


async def test_past_max_sites_the_least_recently_used_site_is_closed():
    pages = {f"https://{h}.test/": (200, ARTICLE) for h in "abc"}
    browser, _log, made = slow_browser(pages, max_sites=2)
    for h in "abac":
        await browser.fetch(f"https://{h}.test/")
    assert set(browser._contexts) == {"a.test", "c.test"}
    assert made["b.test"][0].closed and not made["a.test"][0].closed and not made["c.test"][0].closed


async def test_a_site_being_read_is_never_closed_under_its_reader():
    pages = {"https://slow.test/": (200, ARTICLE), "https://fast.test/": (200, ARTICLE)}
    browser, _log, made = slow_browser(pages, slow_sites=frozenset({"slow.test"}), max_sites=1, idle_close=0.01)
    slow = asyncio.ensure_future(browser.fetch("https://slow.test/"))
    await asyncio.sleep(0.07)  # the slow site is launched and mid-read
    await browser.fetch("https://fast.test/")  # over max_sites, and the idle reaper runs every 5 ms
    assert not slow.done() and not made["slow.test"][0].closed
    assert made["fast.test"][0].closed  # the one past the cap that nobody is reading
    assert (await slow).status == 200


async def test_a_site_unused_for_idle_close_is_closed_and_relaunched_on_next_use():
    browser, log, made = slow_browser({"https://idle.test/": (200, ARTICLE)}, idle_close=0.05)
    await browser.fetch("https://idle.test/")
    await _until(log, ("context-close",))
    assert made["idle.test"][0].closed and not browser.connected
    await browser.fetch("https://idle.test/")
    assert [e for e in log if e[0] == "launch"] == [("launch", "idle.test")] * 2
    reaper = browser._reaper
    await browser.close()
    await asyncio.gather(reaper, return_exceptions=True)
    assert reaper.cancelled()


async def _until(log: list, entry: tuple) -> None:
    for _ in range(200):
        if entry in log:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{entry} never happened")


async def test_a_site_relaunched_while_it_closes_waits_for_the_close():
    pages = {"https://a.test/": (200, ARTICLE), "https://b.test/": (200, ARTICLE)}
    browser, log, _made = slow_browser(pages, slow_close=frozenset({"a.test"}), max_sites=1)
    await browser.fetch("https://a.test/")
    evicting = asyncio.ensure_future(browser.fetch("https://b.test/"))  # past max_sites: a closes, slowly
    await _until(log, ("close-start", "a.test"))
    await browser.fetch("https://a.test/")  # the same profile: its launch must wait for the close
    await evicting
    relaunch = [i for i, e in enumerate(log) if e == ("launch", "a.test")][1]
    assert log.index(("close-done", "a.test")) < relaunch


async def test_an_interrupted_read_still_finishes_closing_the_site_it_evicts():
    pages = {"https://a.test/": (200, ARTICLE), "https://b.test/": (200, ARTICLE)}
    browser, log, made = slow_browser(pages, slow_close=frozenset({"a.test"}), max_sites=1)
    await browser.fetch("https://a.test/")
    evicting = asyncio.ensure_future(browser.fetch("https://b.test/"))
    await _until(log, ("close-start", "a.test"))
    evicting.cancel()  # the turn is interrupted while b's read closes a
    await asyncio.gather(evicting, return_exceptions=True)
    await _until(log, ("close-done", "a.test"))  # a half-closed Chromium would keep a's profile locked
    assert made["a.test"][0].closed


class _Sink(BaseHTTPRequestHandler):
    """A loopback service the page must not reach: it records every request it gets."""

    requests: list[str] = []

    def do_GET(self):  # noqa: N802
        self.requests.append(self.path)
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args) -> None:
        return None


async def test_real_browser_aborts_loopback_subresources_popups_and_websockets(tmp_path, monkeypatch, local_site):
    """The page's own origin is exempt (as the configured SearXNG would be); a second loopback port is not."""
    _playwright_or_skip()
    import asyncio

    _Sink.requests = []
    sink = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    other = f"http://127.0.0.1:{sink.server_address[1]}"
    base, _requests = local_site
    _Site.page_override = (
        f'<html><body><img src="{other}/img"><script>fetch("{other}/fetch").catch(() => 0);'
        f'try {{ new WebSocket("{other.replace("http", "ws")}/ws"); }} catch (e) {{}}'
        f'window.open("{other}/popup");</script><main>{"word " * 400}</main></body></html>'
    )
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3"))
    origin = ("http", "127.0.0.1", int(base.rsplit(":", 1)[1]))
    manager = BrowserManager(challenge_wait=0, allow_origins=frozenset({origin}))
    try:
        page = await _launch_or_skip(manager, f"{base}/guarded")
        await asyncio.sleep(1.5)  # the img, fetch, socket and popup all start while the page loads
    finally:
        _Site.page_override = None
        await manager.close()
        sink.shutdown()
        sink.server_close()
    assert page.status == 200
    assert _Sink.requests == []
