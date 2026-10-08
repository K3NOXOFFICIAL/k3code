"""The browser tool: classification, escalation from fetch, the stop rules, profiles and the environment.

The stop rules are checked with a fake browser whose page refuses every action except navigation, so a CAPTCHA,
login or paywall can only end the read. The real-Chromium tests run against a local HTTP server and are skipped,
with the reason printed, when Playwright or its Chromium is not installed.
"""

from __future__ import annotations

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
CHALLENGE = ("<html><head><title>Attention Required! | Cloudflare</title></head><body>"
             "<h1>Attention Required! | Cloudflare</h1></body></html>")
CAPTCHA = '<html><body><form><div class="g-recaptcha" data-sitekey="x"></div></form></body></html>'
PAYWALL = ("<html><body><main>" + ("teaser words " * 120) +
           "<p>Subscribe to continue reading this article.</p></main></body></html>")
LOGIN = "<html><body><h1>Please log in to view this page</h1><input type=password></body></html>"


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

    async def new_page(self) -> FakePage:
        return FakePage(self._pages, self.log)

    async def close(self) -> None:
        self.log.append(("context-close",))


def fake_browser(pages: dict[str, tuple[int, str]]) -> tuple[BrowserManager, list]:
    log: list = []

    async def launcher(site: str):
        log.append(("launch", site))
        return FakeContext(pages, log)

    return BrowserManager(challenge_wait=0, launcher=launcher), log


def fetcher(**kw) -> WebFetcher:
    return WebFetcher(respect_robots=False, **kw)


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
    defs = re.compile(r"^\s*(?:def|class)\s+_*(?:solve|stealth|spoof|bypass|residential|proxy_?pool)\w*"
                      r"|^\s*(?:def|class)\s+\w*(?:captcha|solver)\w*", re.I | re.MULTILINE)
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


async def test_the_agent_browse_tool_reports_stops_and_missing_playwright(monkeypatch):
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

    monkeypatch.setattr(b, "playwright_module", missing)
    reg = _Registry()
    register_web_tools(reg, SimpleNamespace(research={}), browser=BrowserManager(challenge_wait=0))
    out = await reg.handlers["web_browse"]({"url": "https://ok.test/a"})
    assert "unavailable" in out["error"] and "playwright" in out["error"].lower()


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
    manager = BrowserManager(challenge_wait=0)
    try:
        await _launch_or_skip(manager, f"{base}/article")
        title, text = await fetch_page(f"{base}/gate", fetcher=fetcher(), browser=manager)
    finally:
        await manager.close()
    assert title == "Real Title" and "word word" in text
    assert ("GET", "/gate", next(ua for _m, p, ua in requests if p == "/gate")) in requests
    assert (tmp_path / "k3" / "browser" / "profiles" / "127.0.0.1").is_dir()  # the profile lives under k3code


async def test_real_browser_stops_at_a_captcha_and_posts_nothing(tmp_path, monkeypatch, local_site):
    _playwright_or_skip()
    base, requests = local_site
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3"))
    manager = BrowserManager(challenge_wait=0)
    try:
        await _launch_or_skip(manager, f"{base}/article")
        with pytest.raises(Stopped, match="CAPTCHA"):
            await fetch_page(f"{base}/captcha", fetcher=fetcher(), browser=manager)
    finally:
        await manager.close()
    assert requests and all(method == "GET" for method, _p, _ua in requests)


async def test_real_browser_saves_a_download_under_the_k3code_home_only(tmp_path, monkeypatch, local_site):
    _playwright_or_skip()
    base, _requests = local_site
    k3home = tmp_path / "k3"
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    manager = BrowserManager(challenge_wait=0)
    try:
        page = await _launch_or_skip(manager, f"{base}/article")
        assert page.status == 200
        got = await manager.fetch(f"{base}/download")
    finally:
        await manager.close()
    assert len(got.downloads) == 1
    saved = Path(got.downloads[0])
    assert saved.is_relative_to(k3home.resolve()) and saved.read_text() == "quarterly figures\n"
