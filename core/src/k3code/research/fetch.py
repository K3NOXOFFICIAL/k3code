"""Shared web fetch layer for the agent tools and /ultraresearch.

One ``WebFetcher`` per gateway: a pooled httpx client, a TTL cache of successful pages, a per-host token bucket, a
whole-request deadline (the body is cut when it runs out) and robots.txt, honoured by default. Clock and sleep are
injectable so the rate and deadline tests run on a fake clock.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

MAX_FETCH_BYTES = 2 * 1024 * 1024  # how much of a response body is read at most
ROBOTS_MAX_BYTES = 512 * 1024
UA = "k3code-research/0.1 (+https://github.com/K3NOXOFFICIAL/k3code)"
#: The product token robots.txt rules are matched against (``User-agent: k3code-research``).
ROBOTS_TOKEN = "k3code-research"
#: Realistic request headers. The User-Agent stays honest: sites that want a real browser get the browser tool.
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}
ROBOTS_TTL = 3600.0
ROBOTS_RETRY = 300.0  # an unreadable robots.txt is retried after this long
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]


class FetchRefused(RuntimeError):
    """robots.txt disallows the URL, or the URL is not http(s)."""


class FetchDeadline(RuntimeError):
    """The whole-request deadline ran out before the response headers arrived."""


class FetchStatus(RuntimeError):
    """A non-2xx answer. ``status`` lets the caller decide (403 and challenge pages escalate to the browser)."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    content_type: str
    body: str
    truncated: bool = False  # the deadline or the byte cap cut the body
    cached: bool = False


def _safe_url(url: str) -> str | None:
    u = urlparse(url)
    return url if u.scheme in ("http", "https") and u.netloc else None


class WebFetcher:
    def __init__(
        self,
        *,
        ttl: float = 900.0,
        per_host_rate: float = 1.0,
        burst: float = 1.0,
        deadline: float = 20.0,
        respect_robots: bool = True,
        max_bytes: int = MAX_FETCH_BYTES,
        cache_size: int = 256,
        client: httpx.AsyncClient | None = None,
        now: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.ttl, self.deadline, self.respect_robots, self.max_bytes = ttl, deadline, respect_robots, max_bytes
        self.per_host_rate, self.burst, self.cache_size = max(per_host_rate, 1e-6), max(burst, 1.0), cache_size
        self._client = client
        self._owns_client = client is None
        self._now, self._sleep = now, sleep
        self._cache: dict[str, tuple[float, Fetched]] = {}
        self._buckets: dict[str, tuple[float, float]] = {}  # host -> (tokens, last seen)
        self._robots: dict[str, tuple[float, RobotFileParser | None, bool]] = {}  # origin -> (expires, rules, deny)

    @classmethod
    def from_config(cls, research: dict[str, Any] | None) -> WebFetcher:
        cfg = dict(research or {})
        return cls(
            ttl=float(cfg.get("fetch_cache_ttl", 900)),
            per_host_rate=float(cfg.get("fetch_rate_per_host", 1.0)),
            deadline=float(cfg.get("fetch_deadline", 20)),
            respect_robots=bool(cfg.get("respect_robots", True)),
        )

    def _http(self) -> httpx.AsyncClient:
        # created on first use, inside the running loop: a client made at import or in __init__ would be bound to the
        # wrong event loop once pytest (or a restarted gateway) runs another one
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(follow_redirects=True, timeout=self.deadline, headers=HEADERS)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def admit(self, url: str) -> None:
        """robots.txt and the per-host rate for a navigation made outside ``get()`` (the browser tool)."""
        if _safe_url(url) is None:
            raise ValueError(f"not an http(s) URL: {url}")
        if self.respect_robots and (reason := await self._robots_refusal(url, self._now() + self.deadline)):
            raise FetchRefused(reason)
        await self._throttle(urlparse(url).netloc.lower())

    async def get(self, url: str) -> Fetched:
        """A page as text. Non-2xx answers come back as ``Fetched`` so the caller can escalate; a refused URL raises."""
        if _safe_url(url) is None:
            raise ValueError(f"not an http(s) URL: {url}")
        if (hit := self._cache.get(url)) and hit[0] > self._now():
            return replace(hit[1], cached=True)
        deadline_at = self._now() + self.deadline
        if self.respect_robots and (reason := await self._robots_refusal(url, deadline_at)):
            raise FetchRefused(reason)
        await self._throttle(urlparse(url).netloc.lower())
        got = await self._stream(url, deadline_at, self.max_bytes)
        if 200 <= got.status < 300 and not got.truncated:
            self._remember(url, got)
        return got

    # ── per-domain token bucket ──

    async def _throttle(self, host: str) -> None:
        while True:
            now = self._now()
            tokens, last = self._buckets.get(host, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.per_host_rate)
            if tokens >= 1:
                self._buckets[host] = (tokens - 1, now)
                return
            self._buckets[host] = (tokens, now)
            await self._sleep((1 - tokens) / self.per_host_rate)

    # ── TTL cache ──

    def _remember(self, url: str, got: Fetched) -> None:
        self._cache.pop(url, None)
        self._cache[url] = (self._now() + self.ttl, got)
        while len(self._cache) > self.cache_size:
            self._cache.pop(next(iter(self._cache)))

    # ── robots.txt ──

    async def _robots_refusal(self, url: str, deadline_at: float) -> str | None:
        """Why robots.txt keeps this URL out, or None when it may be fetched. Rules are cached per origin."""
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        now = self._now()
        hit = self._robots.get(origin)
        if hit is None or hit[0] <= now:
            rules, unreadable = await self._load_robots(origin, deadline_at)
            self._robots[origin] = (now + (ROBOTS_RETRY if unreadable else ROBOTS_TTL), rules, unreadable)
            hit = self._robots[origin]
        _, rules, unreadable = hit
        if unreadable:
            return f"robots.txt at {origin} could not be read; {url} not fetched"
        if rules is not None and not rules.can_fetch(ROBOTS_TOKEN, url):
            return f"robots.txt disallows {url} for {ROBOTS_TOKEN}; not fetched"
        return None

    async def _load_robots(self, origin: str, deadline_at: float) -> tuple[RobotFileParser | None, bool]:
        """(rules, unreadable). No robots.txt, or a 4xx other than 429, allows everything; an unreadable one (5xx,
        429, a timeout) refuses for a while, as the crawlers do."""
        try:
            got = await self._stream(f"{origin}/robots.txt", deadline_at, ROBOTS_MAX_BYTES)
        except (httpx.HTTPError, FetchDeadline, TimeoutError):
            return None, True
        if 200 <= got.status < 300:
            rules = RobotFileParser()
            rules.parse(got.body.splitlines())
            return rules, False
        if 400 <= got.status < 500 and got.status != 429:
            return None, False
        return None, True

    # ── the request itself ──

    async def _stream(self, url: str, deadline_at: float, max_bytes: int) -> Fetched:
        """Streams the body up to ``max_bytes`` and the deadline. A deadline hit after the headers keeps the prefix
        (``truncated``); one hit before them raises ``FetchDeadline``."""
        remaining = deadline_at - self._now()
        if remaining <= 0:
            raise FetchDeadline(f"deadline reached before {url} was requested")
        client = self._http()
        buf = bytearray()
        status, ctype, encoding, truncated = 0, "", "utf-8", False
        try:
            async with asyncio.timeout(remaining):  # a stalled read is cut here, not only between chunks
                async with client.stream("GET", url) as r:
                    status, ctype = r.status_code, r.headers.get("content-type", "")
                    encoding = r.encoding or "utf-8"
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) >= max_bytes:
                            truncated = True
                            break
                        if self._now() >= deadline_at:  # a trickle is cut at the deadline even when chunks keep coming
                            truncated = True
                            break
        except TimeoutError as e:
            if not status:
                raise FetchDeadline(f"deadline reached waiting for {url}") from e
            truncated = True
        body = bytes(buf[:max_bytes]).decode(encoding, errors="replace")
        return Fetched(url=url, status=status, content_type=ctype, body=body, truncated=truncated)
