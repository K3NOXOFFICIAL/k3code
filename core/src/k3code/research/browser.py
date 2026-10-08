"""Browser for pages plain HTTP cannot read: a real Chromium (Playwright, optional), one profile per site.

``web_fetch`` hands a page here when the HTTP answer is a 403 or a challenge interstitial. The browser reads what
renders on its own. It does not solve, click or type: a CAPTCHA, a login wall or a paywall stops it with a report.
Out of scope, enforced by tests: CAPTCHA solving, login automation, paywall bypass, stealth fingerprint spoofing and
residential proxies.

The browser is a trusted process outside bwrap. It gets a scrubbed environment (no provider keys, no K3CODE_*), its
own HOME inside the k3code home, and every profile and download lives under the k3code home. It never attaches to a
running browser unless ``browser.cdp_url`` is set (see the gateway's browser.manage).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from k3code.paths import home as k3code_home
from k3code.router.classifier import UPSTREAM_BLOCKED_PATTERNS


class Verdict(StrEnum):
    OK = "ok"
    BLOCKED = "blocked"  # a 403 or a challenge interstitial: a real browser may get through on its own
    CAPTCHA = "captcha"  # stop: never solved
    LOGIN = "login"  # stop: never logged in to
    PAYWALL = "paywall"  # stop: never bypassed
    ERROR = "error"


STOP_VERDICTS = frozenset({Verdict.CAPTCHA, Verdict.LOGIN, Verdict.PAYWALL})

CAPTCHA_PATTERNS = (
    "g-recaptcha",
    "grecaptcha",
    "h-captcha",
    "hcaptcha.com",
    "recaptcha/api",
    "captcha-container",
    "verify you are human",
    "prove you are human",
    "are you a robot",
    "i'm not a robot",
    "i am not a robot",
    "complete the captcha",
    "solve the captcha",
)
LOGIN_PATTERNS = (
    "sign in to continue",
    "log in to continue",
    "login to continue",
    "sign in to read",
    "log in to read",
    "please log in to",
    "please sign in to",
    "you must be logged in",
    "you need to log in to",
)
PAYWALL_PATTERNS = (
    "subscribe to continue",
    "subscribe to read",
    "subscribers only",
    "for subscribers only",
    "this article is for subscribers",
    "to continue reading, subscribe",
    "become a member to continue",
    "you have reached your free article limit",
)
CHALLENGE_PATTERNS = UPSTREAM_BLOCKED_PATTERNS + ("just a moment...", "checking your browser before accessing")
#: A CAPTCHA widget on a page with at least this many visible words is a form on a real page, not a gate in front of it.
GATE_WORDS = 150

INSTALL_HINT = (
    "the browser tool needs the optional Playwright package: install k3code with the browser extra and run "
    "`playwright install chromium`; web_fetch keeps working without it"
)


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium is missing: the browser tool says so, and plain fetch still works."""


class Stopped(RuntimeError):
    """The page is a CAPTCHA, a login wall or a paywall. The message is the report for the user."""


@dataclass(frozen=True)
class RenderedPage:
    url: str  # the URL after redirects and scripts
    status: int  # the status of the navigation; 0 when there was no HTTP response (a download)
    html: str
    downloads: tuple[str, ...] = ()


def _has(low: str, patterns: tuple[str, ...]) -> bool:
    return any(p in low for p in patterns)


def visible_words(html: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", html).split())


def classify(status: int, body: str) -> Verdict:
    """What an answer is: readable, blocked (the browser may get through), or a stop. Pure, no browser needed."""
    low = body.lower()
    if _has(low, CAPTCHA_PATTERNS) and visible_words(body) < GATE_WORDS:
        return Verdict.CAPTCHA
    if status == 402 or _has(low, PAYWALL_PATTERNS):
        return Verdict.PAYWALL
    if status == 401 or _has(low, LOGIN_PATTERNS):
        return Verdict.LOGIN
    if status == 403 or _has(low, CHALLENGE_PATTERNS):
        return Verdict.BLOCKED
    if 200 <= status < 300:
        return Verdict.OK
    return Verdict.ERROR


def stop_report(verdict: Verdict, url: str) -> str:
    """The report for a stop: what was found, what was not done."""
    if verdict is Verdict.CAPTCHA:
        return (
            f"stopped at a CAPTCHA on {url}: k3code does not solve CAPTCHAs. Open the page in your own browser, "
            "or use another source."
        )
    if verdict is Verdict.LOGIN:
        return (
            f"stopped at a login wall on {url}: k3code does not log in to sites. Use another source, or read it "
            "in your own session."
        )
    return f"stopped at a paywall on {url}: k3code does not bypass paywalls. Use another source."


def playwright_module() -> Any:
    """``playwright.async_api``, or BrowserUnavailable. Tests replace this to simulate a missing package."""
    try:
        return importlib.import_module("playwright.async_api")
    except ImportError as e:
        raise BrowserUnavailable(INSTALL_HINT) from e


def _site_slug(site: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "_", (site or "").lower()).strip("._")[:120] or "site"


def profile_dir(site: str, home: Path | None = None) -> Path:
    """The persistent profile of one site, always inside the k3code home."""
    root = Path(home or k3code_home()).expanduser().resolve()
    path = root / "browser" / "profiles" / _site_slug(site)
    if not path.resolve().is_relative_to(root):  # defensive: the slug cannot escape, but say so if it ever does
        raise ValueError("browser profile outside the k3code home")
    return path


def downloads_dir(home: Path | None = None) -> Path:
    return Path(home or k3code_home()).expanduser() / "browser" / "downloads"


def scrubbed_env(home: Path | None = None) -> dict[str, str]:
    """The browser's environment: nothing from the parent but PATH and a locale. HOME points inside the k3code home."""
    root = Path(home or k3code_home()).expanduser()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root / "browser" / "home"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }


class BrowserManager:
    """One Chromium per gateway, with a persistent context per site. Launched on first use, never at start-up."""

    def __init__(
        self,
        home: Path | None = None,
        *,
        headless: bool = True,
        executable_path: str = "",
        challenge_wait: float = 8.0,
        cdp_url: str = "",
        launcher: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.home = home
        self.headless = headless
        self.executable_path = executable_path
        self.challenge_wait = challenge_wait
        #: ``browser.cdp_url``: empty (the default) means k3code never attaches to a running browser
        self.cdp_url = cdp_url
        self._launch = launcher or self._launch_playwright
        self._contexts: dict[str, Any] = {}
        self._cdp: Any = None  # the browser attached over CDP, when browser.manage connected to browser.cdp_url
        self._pw: Any = None

    @classmethod
    def from_config(cls, config: Any) -> BrowserManager:
        cfg = dict(getattr(config, "browser", None) or {})
        return cls(
            headless=bool(cfg.get("headless", True)),
            executable_path=str(cfg.get("executable_path") or ""),
            challenge_wait=float(cfg.get("challenge_wait", 8)),
            cdp_url=str(cfg.get("cdp_url") or "").strip(),
        )

    # ── browser.manage: inspect, attach to the configured CDP endpoint, or drop it ──

    @property
    def connected(self) -> bool:
        return self._cdp is not None or bool(self._contexts)

    def _state(self, messages: list[str]) -> dict[str, Any]:
        if self._contexts and self._cdp is None:
            messages = [*messages, f"k3code's own browser is running for {len(self._contexts)} site(s)"]
        return {
            "connected": self.connected,
            "url": self.cdp_url if self._cdp is not None else None,
            "messages": messages,
        }

    async def manage(self, action: str, url: str | None = None) -> dict[str, Any]:
        """``status`` never starts a browser. ``connect`` attaches only to ``browser.cdp_url``, which is off by default:
        a URL from the client that differs from it is refused. ``disconnect`` drops the attachment and the sites."""
        if action == "status":
            return self._state([])
        if action == "disconnect":
            await self.close()
            return self._state(["browser disconnected"])
        if not self.cdp_url:
            return self._state(
                [
                    "CDP attach is off by default: set browser.cdp_url in config.yaml to a Chromium started with "
                    "--remote-debugging-port. k3code's own browser starts on the first web_browse or escalated fetch."
                ]
            )
        wanted = (url or "").strip().rstrip("/")
        if wanted and wanted != self.cdp_url.rstrip("/"):
            return self._state(["refused: the endpoint is not the configured browser.cdp_url"])
        try:
            await self.attach_cdp(self.cdp_url)
        except BrowserUnavailable as e:
            return self._state([str(e)])
        except Exception as e:  # noqa: BLE001 - the endpoint is config: the message says what failed, not where
            return self._state([f"could not attach to the configured browser ({type(e).__name__})"])
        return self._state(["browser connected via CDP to browser.cdp_url"])

    async def attach_cdp(self, endpoint: str) -> None:
        mod = playwright_module()
        if self._pw is None:
            self._pw = await mod.async_playwright().start()
        self._cdp = await self._pw.chromium.connect_over_cdp(endpoint)

    async def _launch_playwright(self, site: str) -> Any:
        mod = playwright_module()
        if self._pw is None:
            self._pw = await mod.async_playwright().start()
        profile = profile_dir(site, self.home)
        profile.mkdir(parents=True, exist_ok=True)
        downloads_dir(self.home).mkdir(parents=True, exist_ok=True)
        env = scrubbed_env(self.home)
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        opts: dict[str, Any] = {"headless": self.headless, "accept_downloads": True, "env": env}
        if self.executable_path:
            opts["executable_path"] = self.executable_path
        try:
            return await self._pw.chromium.launch_persistent_context(str(profile), **opts)
        except Exception as e:  # noqa: BLE001
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e).lower():
                raise BrowserUnavailable(
                    "Chromium for Playwright is not installed: run `playwright install chromium`"
                ) from e
            raise

    async def context_for(self, site: str) -> Any:
        if self._cdp is not None:  # attached by the user's own choice: the browser's own context, no k3code profile
            return self._cdp.contexts[0] if self._cdp.contexts else await self._cdp.new_context()
        if site not in self._contexts:
            self._contexts[site] = await self._launch(site)
        return self._contexts[site]

    async def fetch(self, url: str) -> RenderedPage:
        """Renders ``url`` and returns what the page shows. Reads only: no clicks, no typing, no form submits."""
        site = urlparse(url).hostname or ""
        ctx = await self.context_for(site)
        page = await ctx.new_page()
        saved: list[str] = []
        pending: list[asyncio.Future[Any]] = []
        page.on("download", lambda d: pending.append(asyncio.ensure_future(self._save_download(d, saved))))
        try:
            return await self._read(page, url, saved, pending)
        finally:
            with suppress(Exception):
                await page.close()

    async def _read(self, page: Any, url: str, saved: list[str], pending: list[asyncio.Future[Any]]) -> RenderedPage:
        status = 0
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            status = int(getattr(resp, "status", 0) or 0) if resp is not None else 0
        except Exception as e:  # noqa: BLE001 - a file download aborts the navigation: that is an answer, not a crash
            if "Download is starting" not in str(e):
                raise
            for _ in range(100):  # the download event arrives just after the navigation is aborted
                if pending:
                    break
                await asyncio.sleep(0.1)
        if pending:
            await asyncio.wait(pending, timeout=60)
        html = await page.content() if not saved else ""
        if status >= 400 and _has(html.lower(), CHALLENGE_PATTERNS):
            # a JS challenge often clears by itself in a real browser: give it a few seconds, read nothing else
            for _ in range(int(self.challenge_wait)):
                await asyncio.sleep(1.0)
                html = await page.content()
                if not _has(html.lower(), CHALLENGE_PATTERNS):
                    status = 200  # the challenge passed; the document now is the page itself
                    break
        return RenderedPage(url=str(getattr(page, "url", url) or url), status=status, html=html, downloads=tuple(saved))

    async def _save_download(self, download: Any, saved: list[str]) -> None:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(download.suggested_filename or "download"))[:120] or "download"
        dest = downloads_dir(self.home) / f"{secrets.token_hex(4)}-{name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        await download.save_as(str(dest))
        saved.append(str(dest))

    async def close(self) -> None:
        for ctx in list(self._contexts.values()):
            with suppress(Exception):
                await ctx.close()
        self._contexts.clear()
        self._cdp = None  # the user's own browser keeps running: only the connection is dropped
        if self._pw is not None:
            with suppress(Exception):
                await self._pw.stop()
            self._pw = None
