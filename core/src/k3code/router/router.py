"""The fallback chain walk: retry/backoff/failover rules over provider×model entries.

Rules (from the M0 task spec):
- network, timeout, server, rate_limit (and unknown): retry the same entry with
  jittered backoff (max ``max_retries``), then fail over to the next entry.
- auth, quota or bad_request: no retries, fail over immediately to the next
  entry (a bad request/unknown model is entry-specific; a different
  model/provider may still work).
- A Retry-After longer than ``max_inline_wait`` (default 20 s) never sleeps inline: the entry goes
  into cooldown until the reset and the walk fails over at once. Quota errors do the same, with
  ``quota_cooldown`` (default 1 h) when the provider declares no reset.
- context_overflow: raise :class:`ContextOverflow` — the loop compacts later.
- When every entry in the chain has failed: :class:`AllProvidersUnreachable` if
  every failure was a network error, otherwise :class:`ChainExhausted` (carrying
  ``retry_after``/``until`` when every entry is cooling down).

Events go through the ``on_event`` callback (v0: dict payloads; structured
schemas come in M1+):
- ``router.attempt``: one call attempt (initial or retry) on an entry.
- ``router.retry``: a retryable failure will be retried on the *same* entry
  after a backoff delay.
- ``router.failover``: the walk is moving on to the *next* entry.
- ``router.exhausted``: every entry in the chain has failed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from k3code.errors import AllProvidersUnreachable, ChainExhausted, ContextOverflow
from k3code.providers.base import Provider
from k3code.providers.retry_utils import jittered_backoff
from k3code.providers.types import Message, StreamEvent, ToolSpec
from k3code.router.classifier import (
    FailoverReason,
    classify_api_error,
    summarize,
)
from k3code.router.cooldown import CooldownStore

logger = logging.getLogger(__name__)

# ── Chain entries ──────────────────────────────────────────────────────


@dataclass
class ChainEntry:
    """One (provider, model) leaf in the normalized fallback chain."""

    provider: Provider
    model: str
    #: Position of the provider block in the chain (for identity/cooldown keys).
    provider_index: int = 0

    @property
    def provider_name(self) -> str:
        return getattr(self.provider, "name", "") or f"provider#{self.provider_index}"

    @property
    def base_url(self) -> str:
        return getattr(self.provider, "base_url", "") or ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider_name.lower(), self.model.lower(), self.base_url.lower())


def build_chain(providers: Sequence[Provider], models: Sequence[str | Sequence[str]]) -> list[ChainEntry]:
    """Expand provider blocks × model lists into a flat ordered walk.

    ``models[i]`` is the model list for ``providers[i]``: a single model or several
    (per-provider fallback order). Later provider blocks only run after every model
    of every earlier block has been tried.
    """
    chain: list[ChainEntry] = []
    for provider_index, (provider, model_spec) in enumerate(zip(providers, models, strict=False)):
        model_list = [model_spec] if isinstance(model_spec, str) else list(model_spec or [])
        for model in model_list:
            chain.append(ChainEntry(provider=provider, model=model, provider_index=provider_index))
    return chain


# ── Events ─────────────────────────────────────────────────────────────


@dataclass
class RouterEvent:
    """One structured router event for the callback / event log."""

    kind: str  # "router.attempt" | "router.retry" | "router.failover" | "router.exhausted"
    provider: str = ""
    model: str = ""
    attempt: int = 0
    reason: str = ""
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.kind

    def as_dict(self) -> dict[str, Any]:
        data = {
            "event": self.kind,
            "provider": self.provider,
            "model": self.model,
            "attempt": self.attempt,
            "reason": self.reason,
            "detail": self.detail,
        }
        if self.extra:
            data.update(self.extra)
        return data


EventCallback = Callable[[RouterEvent], None]


# ── Router ─────────────────────────────────────────────────────────────


class Router:
    """Stream one completion through the fallback chain.

    ``max_retries`` is the per-entry retry budget for retryable reasons
    (network/timeout/server/rate_limit/unknown) before failing over.
    """

    def __init__(
        self,
        chain: Sequence[ChainEntry],
        *,
        max_retries: int = 2,
        base_delay: float = 2.0,
        max_delay: float = 30.0,
        cooldowns: CooldownStore | None = None,
        on_event: EventCallback | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        tier: str = "main",
        max_inline_wait: float = 20.0,
        quota_cooldown: float = 3600.0,
    ) -> None:
        self.chain = list(chain)
        #: longest Retry-After (seconds) worth sleeping on; longer ones cool the entry down and fail over
        self.max_inline_wait = max_inline_wait
        #: cooldown for quota errors that declare no reset
        self.quota_cooldown = quota_cooldown
        #: M4a: model tier this router serves; tagged on every event.
        self.tier = tier
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.cooldowns = cooldowns if cooldowns is not None else CooldownStore()
        self.on_event = on_event
        self._sleep = sleep if sleep is not None else asyncio.sleep

    # ── public API ──

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield stream events from the first entry that completes its handshake.

        The walk state machine runs while yielding: any ProviderError after
        streaming started terminates the stream (partial outputs are not replayed).
        """
        async for event in self._walk(messages, tools, model=model, max_tokens=max_tokens, temperature=temperature):
            if event.type == "error":
                # Providers may surface mid-stream failures as error events; treat
                # them like raised exceptions.
                exc = event.message and event.message.content or "provider stream error"
                raise ProviderStreamError(str(exc))  # type: ignore[arg-type]
            yield event
            if event.type == "done":
                return
        # _walk raising an exception propagates naturally.

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> Message:
        """Drain the stream and return the final assistant message."""
        stream = self.stream(messages, tools, model=model, max_tokens=max_tokens, temperature=temperature)
        async for event in stream:
            if event.type == "done" and event.message is not None:
                return event.message
        raise ChainExhausted("provider stream ended without a final message", last_reason="unknown")

    # ── the walk ──

    async def _walk(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
        max_tokens: int,
        temperature: float | None,
    ) -> AsyncIterator[StreamEvent]:
        last_reason: FailoverReason | None = None
        last_detail = ""
        all_network = True  # tracks "every failure so far was a network failure"
        entry_index = 0
        while entry_index < len(self.chain):
            entry = self.chain[entry_index]
            target_model = model if entry_index == 0 and model else entry.model
            skip_reason = self._skip_in_cooldown(entry)
            if skip_reason is not None:
                logger.debug(
                    "skipping %s/%s: cooldown %.0fs remaining (%s)",
                    entry.provider_name,
                    entry.model,
                    skip_reason,
                    "cooldown",
                )
                cooled = self.cooldowns.reason_of(
                    provider=entry.provider_name, model=entry.model, base_url=entry.base_url
                )
                if cooled is not None:
                    last_reason = cooled
                    all_network = all_network and cooled is FailoverReason.network
                entry_index += 1
                continue
            attempt = 0
            while True:
                self._emit("router.attempt", entry, attempt=attempt + 1)
                streamed = False  # this attempt already yielded output the consumer has collected
                try:
                    async for event in entry.provider.stream(
                        messages, tools, target_model, max_tokens=max_tokens, temperature=temperature
                    ):
                        streamed = True
                        yield event
                    # The provider yields its "done" event then closes normally.
                    return
                except Exception as exc:  # noqa: BLE001 - classified below
                    classified = classify_api_error(exc, provider=entry.provider_name, model=target_model)
                    last_reason = classified.reason
                    last_detail = summarize(exc)
                    all_network = all_network and classified.reason is FailoverReason.network
                    if classified.reason is FailoverReason.context_overflow:
                        raise ContextOverflow(summarize(exc, limit=500)) from exc
                    if streamed:
                        # The next attempt (same entry or the next one) streams the answer from the start: tell the
                        # consumer to drop what it has, or it would see "Hello Hello world".
                        streamed = False
                        yield StreamEvent(type="reset")
                    cooldown = self._cooldown_seconds(classified)
                    if cooldown is not None or classified.immediate_failover:
                        self._failover(
                            entry,
                            target_model,
                            classified.reason,
                            attempt,
                            exc,
                            entry_index=entry_index,
                            retry_after=cooldown,
                        )
                        break
                    # retryable: same entry with backoff, max_retries times, then fail over
                    if attempt >= self.max_retries:
                        self._failover(entry, target_model, classified.reason, attempt, exc, entry_index=entry_index)
                        break
                    attempt += 1
                    delay = self._backoff_for(classified, attempt)
                    logger.info(
                        "retry %d/%d on %s/%s in %.1fs (%s)",
                        attempt,
                        self.max_retries,
                        entry.provider_name,
                        target_model,
                        delay,
                        classified.reason.value,
                    )
                    self._emit(
                        "router.retry",
                        entry,
                        attempt=attempt,
                        reason=classified.reason.value,
                        detail=f"retrying in {delay:.1f}s: {summarize(exc)}",
                        extra={"delay": delay},
                    )
                    await self._sleep(delay)
            entry_index += 1
        # every entry exhausted
        self._emit_exhausted(last_reason)
        if all_network and last_reason is not None:
            raise AllProvidersUnreachable(
                "all provider entries are unreachable (network errors)", attempts=len(self.chain)
            )
        reason_name = last_reason.value if last_reason else "unknown"
        wait = self.earliest_reset()
        if wait is not None:
            until = self.cooldowns.wall() + wait
            clock = time.strftime("%H:%M", time.localtime(until))
            raise ChainExhausted(
                f"all providers rate-limited until {clock} ({reason_name})",
                last_reason=reason_name,
                retry_after=wait,
                until=until,
            )
        detail = f": {last_detail}" if last_detail else ""
        raise ChainExhausted(
            f"all provider entries failed (last reason: {reason_name}){detail}", last_reason=reason_name
        )

    # ── helpers ──

    def _cooldown_seconds(self, classified) -> float | None:
        """Cooldown to arm (and fail over immediately) instead of retrying inline; None = retry as usual."""
        declared = classified.retry_after
        if classified.reason is FailoverReason.quota:
            return float(declared) if declared and declared > 0 else self.quota_cooldown
        if classified.reason is FailoverReason.rate_limit and declared and declared > self.max_inline_wait:
            return float(declared)
        return None

    def earliest_reset(self) -> float | None:
        """Seconds until the first chain entry leaves cooldown, when *every* entry is cooling down."""
        if not self.chain or self.cooldowns is None:
            return None
        remaining = []
        for entry in self.chain:
            if not self.cooldowns.in_cooldown(provider=entry.provider_name, model=entry.model, base_url=entry.base_url):
                return None
            remaining.append(
                self.cooldowns.remaining_seconds(
                    provider=entry.provider_name, model=entry.model, base_url=entry.base_url
                )
            )
        return min(remaining)

    def _backoff_for(self, classified, attempt: int) -> float:
        """Jittered backoff, or the Retry-After window (when short enough to sleep on)."""
        if classified.retry_after is not None and classified.retry_after <= self.max_inline_wait:
            return max(0.0, float(classified.retry_after))
        return jittered_backoff(attempt, base_delay=self.base_delay, max_delay=self.max_delay)

    def _skip_in_cooldown(self, entry: ChainEntry) -> float | None:
        if self.cooldowns is None:
            return None
        if self.cooldowns.in_cooldown(provider=entry.provider_name, model=entry.model, base_url=entry.base_url):
            return self.cooldowns.remaining_seconds(
                provider=entry.provider_name, model=entry.model, base_url=entry.base_url
            )
        return None

    def _failover(
        self,
        entry: ChainEntry,
        target_model: str,
        reason: FailoverReason,
        attempt: int,
        exc: BaseException,
        *,
        entry_index: int,
        retry_after: float | None = None,
    ) -> None:
        # Without a declared long window jittered_backoff already separated the retries; the cooldown
        # then uses its own ladder.
        armed = self.cooldowns.arm(
            reason,
            provider=entry.provider_name,
            model=target_model,
            base_url=entry.base_url,
            retry_after=retry_after,
        )
        if retry_after is not None and armed:
            self._emit(
                "router.cooldown",
                entry,
                attempt=attempt + 1,
                reason=reason.value,
                detail=f"cooling down {armed:.0f}s: {summarize(exc)}",
                extra={"seconds": armed, "until": self.cooldowns.wall() + armed},
            )
        next_index = entry_index + 1
        next_entry = self.chain[next_index] if next_index < len(self.chain) else None
        self._emit(
            "router.failover",
            entry,
            attempt=attempt + 1,
            reason=reason.value,
            detail=summarize(exc),
            extra={"model": next_entry.model} if next_entry is not None else {},
        )

    def _emit_exhausted(self, last_reason: FailoverReason | None) -> None:
        self._emit(
            "router.exhausted",
            None,  # walk-level event, no single entry
            attempt=len(self.chain),
            reason=last_reason.value if last_reason else "unknown",
            detail=f"{len(self.chain)} chain entries exhausted",
        )

    def _emit(
        self,
        kind: str,
        entry: ChainEntry | None = None,
        *,
        attempt: int = 0,
        reason: str = "",
        detail: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self.on_event is None:
            return
        self.on_event(
            RouterEvent(
                kind=kind,
                provider=entry.provider_name if entry else "",
                model=(entry.model if entry else "") or (extra or {}).get("model", ""),
                attempt=attempt,
                reason=reason,
                detail=detail,
                extra={**(extra or {}), "tier": self.tier},
            )
        )


class ProviderStreamError(Exception):
    """A provider surfaced a mid-stream failure as an error event."""
