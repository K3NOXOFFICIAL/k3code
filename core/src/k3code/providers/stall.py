"""A watchdog for streamed model replies: a stream that makes no progress fails as a timeout.

httpx's read timeout restarts on every byte, and the SSE loops skip every line that is not ``data:``, so an upstream
that accepts the request and then sends only comments (``: keepalive``, ``: OPENROUTER PROCESSING``) or Anthropic
``ping`` events never trips it. The watchdog counts only what the model produced: the providers call
:meth:`StreamWatchdog.progress` for a content, tool or usage event and for nothing else.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import AsyncIterator, Awaitable
from typing import TypeVar

import httpx

from k3code.providers.effort import takes_effort

#: Seconds from the request until the first content, tool or usage event.
FIRST_EVENT_TIMEOUT = 60.0
#: Seconds between two such events once the reply has started.
IDLE_TIMEOUT = 120.0
#: Where a reasoning model can be silent for a long time and still be working (the old read timeout).
REASONING_TIMEOUT = 300.0

T = TypeVar("T")


def is_local_endpoint(base_url: str, *, keyless: bool = False) -> bool:
    """Whether the server is one the user runs (a keyless endpoint, a loopback address, ``localhost``, ``*.local``):
    it prefills a long prompt before the first token, which on a modest GPU outlasts the default first-event limit."""
    if keyless:
        return True
    try:
        host = httpx.URL(base_url).host.lower()
    except (httpx.InvalidURL, ValueError):
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class StreamStalled(httpx.ReadTimeout):
    """The upstream took the request but the reply stopped making progress. Classified as ``timeout``."""


def is_stall(exc: BaseException) -> bool:
    """Whether ``exc`` is a stalled stream, or a provider error wrapping one."""
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and len(seen) < 8 and all(cur is not e for e in seen):
        if isinstance(cur, StreamStalled):
            return True
        seen.append(cur)
        cur = cur.__cause__
    return False


class StreamWatchdog:
    """A deadline that every progress event pushes out; waiting past it raises :class:`StreamStalled`."""

    def __init__(self, *, first_event: float, idle: float) -> None:
        self.first_event = first_event
        self.idle = idle
        self._started = False
        self._deadline = time.monotonic() + first_event

    @classmethod
    def for_model(
        cls, kind: str, model: str, *, first_event: float | None = None, idle: float | None = None
    ) -> StreamWatchdog:
        """The limits for one request; an explicit value wins.

        A chat-completions reply to a reasoning model (o-series, gpt-5) carries nothing until the reasoning is done,
        so its first event gets the long limit. An Anthropic stream starts at once, but a long think can show
        nothing but pings, so there the idle limit is the long one.
        """
        reasons = bool(takes_effort(model, kind))
        if first_event is None:
            first_event = REASONING_TIMEOUT if reasons and kind == "openai" else FIRST_EVENT_TIMEOUT
        if idle is None:
            idle = REASONING_TIMEOUT if reasons and kind == "anthropic" else IDLE_TIMEOUT
        return cls(first_event=first_event, idle=idle)

    def progress(self) -> None:
        """The model produced something: the next one is due within the idle limit."""
        self._started = True
        self._deadline = time.monotonic() + self.idle

    async def guard(self, awaitable: Awaitable[T]) -> T:
        """Await ``awaitable`` within the time left, or raise :class:`StreamStalled`."""
        limit = self.idle if self._started else self.first_event
        timer = asyncio.timeout(max(0.0, self._deadline - time.monotonic()))
        try:
            async with timer:
                return await awaitable
        except TimeoutError:
            if not timer.expired():
                raise
            what = f"for {limit:g}s" if self._started else f"within {limit:g}s"
            raise StreamStalled(
                f"stream stalled: no content from the model {what} (comments and pings do not count)"
            ) from None

    async def lines(self, response: httpx.Response) -> AsyncIterator[str]:
        """The response's lines, each awaited under the deadline. Time the consumer spends on a line is not the
        upstream's silence: it is added back to the deadline."""
        lines = response.aiter_lines()
        while (line := await self.guard(anext(lines, None))) is not None:
            handed = time.monotonic()
            yield line
            self._deadline += time.monotonic() - handed
