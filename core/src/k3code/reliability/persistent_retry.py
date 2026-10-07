"""Persistent retry around the router's stream call: pause, park, resume.

Wraps ``Router.stream(...)`` (same async-generator signature) so the agent loop
never loses a turn to transient infrastructure failures:

- ``AllProvidersUnreachable`` + netwatch OFFLINE/CAPTIVE → emit
  ``reliability.paused``, wait until the network is usable again, emit
  ``reliability.resumed`` and retry the same turn (messages are untouched).
- ``AllProvidersUnreachable`` with all registered provider endpoints down (net
  itself fine) → ``park``: exponential backoff 30 s up to 10 min, emitting
  ``reliability.parked(next_retry_at=...)`` / ``reliability.unparked``.
- ``ChainExhausted`` with every entry cooling down (the router fails over past long
  Retry-After / quota windows instead of sleeping) → park until the earliest reset
  (``exc.retry_after``), emitting ``reliability.parked`` with the wall-clock time.
- Other ``ChainExhausted`` with ``last_reason`` ``rate_limit``/``quota`` → park until the
  provider-declared window when known (tracked from ``router.retry`` events), otherwise
  the same backoff ladder.

There is no retry cap by default; ``max_wait`` bounds the total time spent
waiting per ``stream()`` call (None = wait forever, the default). A
:class:`CancelToken` (wired to ``/stop``) aborts any wait immediately with
:class:`TurnCancelled`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from k3code.errors import AllProvidersUnreachable, ChainExhausted, K3CodeError
from k3code.providers.types import Message, StreamEvent, ToolSpec
from k3code.reliability import events as ev
from k3code.reliability.events import EventEmitter
from k3code.reliability.netwatch import NetState, NetWatch
from k3code.router.classifier import FailoverReason

logger = logging.getLogger(__name__)

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


class _RetryAfterObserver:
    """The single ``on_event`` wrapper on a router: forwards every event, tells live retries the Retry-After.

    ``listeners`` holds :class:`PersistentRetry` instances weakly, so loops that are gone cost nothing.
    A provider's Retry-After applies to every caller of that provider, so each live instance hears it.
    """

    def __init__(self, inner: Callable[[Any], None]) -> None:
        self.inner = inner
        self.listeners: weakref.WeakSet[PersistentRetry] = weakref.WeakSet()

    def __call__(self, event: Any) -> None:
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
                for listener in list(self.listeners):
                    listener._last_retry_after = float(delay)
        self.inner(event)


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
        logger.info(
            "all providers unreachable; connectivity monitor: %s", state.value if state is not None else "not running"
        )
        if state is not None and not state.usable_for_llm:
            # Global connectivity lost: pause until the network is back.
            await self._pause_until_usable(state, deadline)
            return True
        # Net is fine(ish) — every provider endpoint is down, or netwatch is
        # unavailable: park with the backoff ladder.
        await self._park(f"all {exc.attempts or 'available'} provider(s) unreachable", deadline, wake_on_recovery=True)
        return True

    async def _handle_exhausted(self, exc: ChainExhausted, deadline: float | None) -> bool:
        if exc.retry_after is not None:  # every entry is cooling down: park until the earliest reset
            # Network-caused cooldowns end when connectivity returns; rate-limit/quota ones only at their reset.
            cooldowns = getattr(self.router, "cooldowns", None)
            network_only = cooldowns is not None and cooldowns.only_network_cooling()
            await self._park(str(exc), deadline, delay=exc.retry_after, until=exc.until, wake_on_recovery=network_only)
            return True
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
        # The failures that armed these cooldowns happened while the network was down; the next attempt must
        # go straight to the provider, not be skipped for the rest of a fixed window.
        self._clear_network_cooldowns()
        self.events.emit(ev.RESUMED, detail=f"network {self.netwatch.state.value if self.netwatch else 'up'}")

    def _clear_network_cooldowns(self) -> None:
        cooldowns = getattr(self.router, "cooldowns", None)
        if cooldowns is not None:
            cooldowns.clear_reason(FailoverReason.network)

    async def _park(
        self,
        detail: str,
        deadline: float | None,
        *,
        delay: float | None = None,
        until: float | None = None,
        wake_on_recovery: bool = False,
    ) -> None:
        """Emit parked, wait out the backoff window (or until connectivity recovers), emit unparked.

        With ``wake_on_recovery`` the wait ends early when the connectivity monitor reports that the
        network (or the providers) came back, and the network cooldowns are cleared so the very next
        attempt reaches the provider instead of being skipped for the rest of a fixed cooldown window.
        """
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
        extra = {} if until is None else {"until": until}
        self.events.emit(ev.PARKED, detail=detail, next_retry_at=next_retry_at, delay=delay, **extra)
        recoveries_at_park = getattr(self.netwatch, "recoveries", 0) if self.netwatch is not None else 0

        def recovered() -> bool:
            return (
                wake_on_recovery
                and self.netwatch is not None
                and getattr(self.netwatch, "recoveries", 0) != recoveries_at_park
            )

        try:
            await self._wait_until(recovered, deadline, until_ts=next_retry_at)
        finally:
            self._raise_if_cancelled()
        woke = recovered()
        if woke:
            self._clear_network_cooldowns()
        self.events.emit(ev.UNPARKED, detail="network recovered; retrying" if woke else "retrying")

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
                raise _MaxWaitExceeded(f"max_wait of {self.config.max_wait:.0f}s exceeded while waiting")
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
        """Subscribe to the router's ``router.retry`` events to remember provider-declared retry windows.

        The router is shared by every agent loop in the process, and a new ``PersistentRetry`` is
        built per loop. Installing one wrapper per instance grew a callback chain by one layer per
        turn until ``RecursionError`` (~1000 turns, found by the 72 h soak), so the router gets one
        :class:`_RetryAfterObserver`; instances register on it by weak reference.
        """
        on_event = getattr(self.router, "on_event", None)
        if on_event is None:
            return
        observer = on_event if isinstance(on_event, _RetryAfterObserver) else None
        if observer is None:
            observer = _RetryAfterObserver(on_event)
            self.router.on_event = observer
        observer.listeners.add(self)
