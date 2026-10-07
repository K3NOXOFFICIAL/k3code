"""Persistent retry around the router's stream call: pause, park, resume.

Wraps ``Router.stream(...)`` (same async-generator signature) so the agent loop
never loses a turn to transient infrastructure failures:

- ``AllProvidersUnreachable`` + netwatch OFFLINE/CAPTIVE → emit
  ``reliability.paused``, wait until the network is usable again, emit
  ``reliability.resumed`` and retry the same turn (messages are untouched).
- ``AllProvidersUnreachable`` with all registered provider endpoints down (net
  itself fine) → ``park``: exponential backoff 30 s up to 10 min, emitting
  ``reliability.parked(next_retry_at=...)`` / ``reliability.unparked``.
- ``ChainExhausted`` with ``last_reason`` ``rate_limit``/``quota`` → park until
  the provider-declared Retry-After/reset window when known (tracked from
  ``router.retry`` events), otherwise the same backoff ladder.

There is no retry cap by default; ``max_wait`` bounds the total time spent
waiting per ``stream()`` call (None = wait forever, the default). A
:class:`CancelToken` (wired to ``/stop``) aborts any wait immediately with
:class:`TurnCancelled`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from k3code.errors import AllProvidersUnreachable, ChainExhausted, K3CodeError
from k3code.providers.types import Message, StreamEvent, ToolSpec
from k3code.reliability import events as ev
from k3code.reliability.events import EventEmitter
from k3code.reliability.netwatch import NetState, NetWatch

#: ChainExhausted reasons that mean "the provider is rate-limiting / quota'd us".
RATE_LIMIT_REASONS = frozenset({"rate_limit", "quota"})


class TurnCancelled(K3CodeError):
    """The user cancelled the turn (via /stop) while it was paused or parked."""


class _MaxWaitExceeded(Exception):
    """Internal: max_wait deadline hit while paused/parked; re-raise the original."""


class CancelToken:
    """Cooperative cancellation flag, aborted from ``/stop``."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass
class RetryConfig:
    """Park/backoff knobs."""

    park_base: float = 30.0  # first all-providers-down park: 30 s
    park_max: float = 600.0  # park ladder cap: 10 min
    backoff_factor: float = 2.0
    #: Total seconds allowed in paused/parked waits per stream() call; None = forever.
    max_wait: float | None = None
    #: Sleep granularity while waiting (keeps waits cancel-aware).
    poll_interval: float = 0.5


class PersistentRetry:
    """Retry wrapper around one :class:`~k3code.router.router.Router`."""

    def __init__(
        self,
        router,
        netwatch: NetWatch | None = None,
        *,
        config: RetryConfig | None = None,
        events: EventEmitter | None = None,
        cancel_token: CancelToken | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.router = router
        self.netwatch = netwatch
        self.config = config or RetryConfig()
        self.events = events or EventEmitter()
        self.cancel_token = cancel_token or CancelToken()
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic
        self._park_step = 0
        # Retry-After window seen on the most recent rate-limit/quota retry
        # (tracked from router.retry events; see _watch_router_events).
        self._last_retry_after: float | None = None
        self._watch_router_events()

    # ── stream wrapper ──

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield router stream events, pausing/parking across infrastructure failures."""
        deadline = None if self.config.max_wait is None else self._monotonic() + self.config.max_wait
        while True:
            self._raise_if_cancelled()
            try:
                async for event in self.router.stream(
                    messages, tools, model=model, max_tokens=max_tokens, temperature=temperature
                ):
                    yield event
                return
            except AllProvidersUnreachable as e:
                try:
                    waited = await self._handle_unreachable(e, deadline)
                except _MaxWaitExceeded:
                    raise e from None  # waited long enough; surface the original failure
                if not waited:
                    raise
            except ChainExhausted as e:
                try:
                    waited = await self._handle_exhausted(e, deadline)
                except _MaxWaitExceeded:
                    raise e from None
                if not waited:
                    raise

    # ── failure handling ──

    async def _handle_unreachable(self, exc: AllProvidersUnreachable, deadline: float | None) -> bool:
        """Pause (offline/captive) or park (all providers down); True when it should retry."""
        state = self.netwatch.state if self.netwatch is not None else None
        if state is not None and not state.usable_for_llm:
            # Global connectivity lost: pause until the network is back.
            await self._pause_until_usable(state, deadline)
            return True
        # Net is fine(ish) — every provider endpoint is down, or netwatch is
        # unavailable: park with the backoff ladder.
        await self._park(f"all {exc.attempts or 'available'} provider(s) unreachable", deadline)
        return True

    async def _handle_exhausted(self, exc: ChainExhausted, deadline: float | None) -> bool:
        if exc.last_reason in RATE_LIMIT_REASONS:
            retry_after = self._last_retry_after
            detail = (
                f"{exc.last_reason} (retry-after {retry_after:.0f}s)"
                if retry_after is not None
                else f"{exc.last_reason}"
            )
            await self._park(detail, deadline, delay=retry_after)
            return True
        return False  # auth, bad_request, ... : not transient, let it propagate

    # ── waits ──

    async def _pause_until_usable(self, state: NetState, deadline: float | None) -> None:
        """Emit paused, block until the net is usable, emit resumed."""
        self.events.emit(ev.PAUSED, detail=f"network {state.value}; waiting to resume")
        try:
            await self._wait_until(
                lambda: self.netwatch is not None and self.netwatch.state.usable_for_llm,
                deadline,
            )
        finally:
            self._raise_if_cancelled()
        self.events.emit(ev.RESUMED, detail=f"network {self.netwatch.state.value if self.netwatch else 'up'}")

    async def _park(self, detail: str, deadline: float | None, *, delay: float | None = None) -> None:
        """Emit parked, wait out the backoff window, emit unparked."""
        if delay is None:
            delay = min(
                self.config.park_base * (self.config.backoff_factor**self._park_step),
                self.config.park_max,
            )
            self._park_step += 1
        else:
            delay = max(0.0, float(delay))
            self._park_step = 0
        next_retry_at = self._monotonic() + delay
        self.events.emit(ev.PARKED, detail=detail, next_retry_at=next_retry_at, delay=delay)
        try:
            await self._wait_until(lambda: False, deadline, until_ts=next_retry_at)
        finally:
            self._raise_if_cancelled()
        self.events.emit(ev.UNPARKED, detail="retrying")

    async def _wait_until(
        self,
        ready: Callable[[], bool],
        deadline: float | None,
        *,
        until_ts: float | None = None,
    ) -> None:
        """Sleep in poll-sized chunks until ``ready()``, ``until_ts`` or ``deadline``.

        Cancel-aware throughout: a cancelled token raises :class:`TurnCancelled`.
        """
        step = self.config.poll_interval
        while True:
            self._raise_if_cancelled()
            now = self._monotonic()
            if deadline is not None and now >= deadline:
                raise _MaxWaitExceeded(
                    f"max_wait of {self.config.max_wait:.0f}s exceeded while waiting"
                )
            if ready() or (until_ts is not None and now >= until_ts):
                return
            chunk = min(step, until_ts - now) if until_ts is not None else step
            if deadline is not None:
                chunk = min(chunk, deadline - now)
            await self._sleep(max(chunk, 0.001))

    def _raise_if_cancelled(self) -> None:
        if self.cancel_token.cancelled:
            raise TurnCancelled("turn cancelled by user (/stop)")

    # ── Retry-After tracking ──

    def _watch_router_events(self) -> None:
        """Wrap the router's on_event to remember provider-declared retry windows."""
        on_event = getattr(self.router, "on_event", None)
        if on_event is None:
            return

        def observed(event: Any) -> None:
            # RouterEvent payloads: kind "router.retry", reason rate_limit/quota,
            # extra {"delay": seconds} (the provider's Retry-After when declared).
            kind = getattr(event, "kind", None) or (event.get("event") if isinstance(event, dict) else None)
            reason = getattr(event, "reason", None) or (event.get("reason") if isinstance(event, dict) else "")
            if kind == "router.retry" and reason in RATE_LIMIT_REASONS:
                extra = getattr(event, "extra", None)
                if extra is None and isinstance(event, dict):
                    extra = event.get("extra")
                delay = (extra or {}).get("delay")
                if isinstance(delay, (int, float)):
                    self._last_retry_after = float(delay)
            on_event(event)

        self.router.on_event = observed
