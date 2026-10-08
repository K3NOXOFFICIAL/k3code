# Vendored from hermes-agent@4127d78da84b1eee105f298979cc57cc7457f98d:agent/fallback_cooldown.py (MIT)
# Copyright (c) 2025 Nous Research
# Adapted for k3code: Hermes arms cooldowns on the agent object; k3code keeps a
# standalone CooldownStore keyed by (provider, model, base_url), so the router can
# consult it without an agent. Retry-After / provider reset windows are honored
# through the vendored retry_utils parsers. k3code addition: network failures also
# cool down (the provider is down for everyone), with a flat configurable window
# instead of the exponential ladder — set ``K3CODE_NETWORK_COOLDOWN_SECONDS=0``
# (or arm with ``network_cooldown=0``) to disable.

"""Per-entry rate-limit cooldowns for the fallback walk.

A failing entry is put into cooldown until the provider's reset window (Retry-After,
body reset fields or free-text reset grammars) — or an exponential floor when the
provider declares nothing. The router skips entries still in cooldown.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.router.classifier import FailoverReason

logger = logging.getLogger(__name__)

# rate_limit/quota use the exponential reset ladder below; network uses a flat
# window (same provider being unreachable is chain-wide news) — still configurable.
_COOLDOWN_REASONS = frozenset({FailoverReason.rate_limit, FailoverReason.quota, FailoverReason.network})

_NETWORK_COOLDOWN_ENV = "K3CODE_NETWORK_COOLDOWN_SECONDS"

# Exponential fallback when the provider declares no reset: 60 s → 2 m → 4 m … capped.
_BASE_COOLDOWN_SECONDS = 60.0
_MAX_COOLDOWN_SECONDS = 14400.0  # 4 h, carried over from Hermes


def network_cooldown_seconds() -> float:
    """Flat cooldown window for network failures (0 disables arming)."""
    raw = os.environ.get(_NETWORK_COOLDOWN_ENV)
    if raw is None:
        return _BASE_COOLDOWN_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _BASE_COOLDOWN_SECONDS
    return max(0.0, value)


def _identity(provider: str, model: str, base_url: str) -> tuple[str, str, str]:
    return (provider.strip().lower(), model.strip().lower(), (base_url or "").strip().lower())


@dataclass
class EntryCooldown:
    """One entry's cooldown window on the monotonic clock."""

    until: float
    seconds: float
    reason: FailoverReason
    #: wall-clock epoch of the reset (what survives a restart in ``cooldowns.json``)
    until_wall: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.until - time.monotonic())


@dataclass
class CooldownStore:
    """Cooldowns keyed by (provider, model, base_url); a probe-safe stand-in for
    Hermes' agent-object fields."""

    entries: dict[tuple[str, str, str], EntryCooldown] = field(default_factory=dict)
    #: when set, rate-limit/quota cooldowns are persisted here so a restart does not hammer the provider
    path: Path | None = None
    #: wall-clock source (injectable for tests)
    wall: Callable[[], float] = time.time
    #: consecutive rate-limit/quota arms per entry (the exponential ladder's step), reset by ``record_success``
    strikes: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self._load()

    # ── persistence ──

    def _load(self) -> None:
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        now_wall, now_mono = self.wall(), time.monotonic()
        for row in raw.get("entries", []) if isinstance(raw, dict) else []:
            try:
                key = _identity(row["provider"], row["model"], row.get("base_url", ""))
                until_wall = float(row["until"])
                reason = FailoverReason(row["reason"])
            except (KeyError, TypeError, ValueError):
                continue
            remaining = until_wall - now_wall
            if remaining > 0:
                self.entries[key] = EntryCooldown(now_mono + remaining, remaining, reason, until_wall)

    def _save(self) -> None:
        if self.path is None:
            return
        now_wall, now_mono = self.wall(), time.monotonic()
        rows = [
            {"provider": k[0], "model": k[1], "base_url": k[2], "reason": e.reason.value, "until": e.until_wall}
            for k, e in self.entries.items()
            if e.reason is not FailoverReason.network and e.until > now_mono and e.until_wall > now_wall
        ]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"entries": rows}))
            tmp.replace(self.path)
        except OSError as e:
            logger.warning("could not persist cooldowns to %s: %s", self.path, e)

    def arm(
        self,
        reason: FailoverReason,
        *,
        provider: str,
        model: str,
        base_url: str = "",
        retry_after: float | None = None,
        backoff_count: int | None = None,
        network_cooldown: float | None = None,
        now: float | None = None,
    ) -> float | None:
        """Put an entry into cooldown until its reset window. Returns the armed seconds.

        ``network_cooldown`` overrides the flat network window (0 disables arming
        on network failures); rate_limit/quota keep the reset/exponential ladder, whose step is the number of
        consecutive arms of this entry unless ``backoff_count`` is given (it always was 0: the ladder never climbed).
        """
        if reason not in _COOLDOWN_REASONS:
            return None
        key = _identity(provider, model, base_url)
        if reason is not FailoverReason.network:
            if backoff_count is None:
                backoff_count = self.strikes.get(key, 0)
            self.strikes[key] = backoff_count + 1
        if reason is FailoverReason.network:
            window = network_cooldown if network_cooldown is not None else network_cooldown_seconds()
            if window <= 0:
                return None
            seconds = math.ceil(window)
        elif provider_delta := _provider_reset_delay(retry_after):
            seconds = math.ceil(provider_delta)
        else:
            seconds = min(_BASE_COOLDOWN_SECONDS * (2 ** min(backoff_count or 0, 16)), _MAX_COOLDOWN_SECONDS)
        monotonic_now = time.monotonic() if now is None else now
        self.entries[key] = EntryCooldown(
            until=monotonic_now + seconds,
            seconds=seconds,
            reason=reason,
            until_wall=self.wall() + seconds,
        )
        self._save()
        return seconds

    def in_cooldown(self, *, provider: str, model: str, base_url: str = "", now: float | None = None) -> bool:
        """Whether this entry is still cooling down."""
        entry = self.entries.get(_identity(provider, model, base_url))
        if entry is None:
            return False
        monotonic_now = time.monotonic() if now is None else now
        if entry.until <= monotonic_now:
            # Expired windows are dropped so a long-lived store never grows unbounded.
            self.entries.pop(_identity(provider, model, base_url), None)
            return False
        return True

    def remaining_seconds(self, *, provider: str, model: str, base_url: str = "", now: float | None = None) -> float:
        entry = self.entries.get(_identity(provider, model, base_url))
        if entry is None:
            return 0.0
        monotonic_now = time.monotonic() if now is None else now
        return max(0.0, entry.until - monotonic_now)

    def reason_of(self, *, provider: str, model: str, base_url: str = "") -> FailoverReason | None:
        entry = self.entries.get(_identity(provider, model, base_url))
        return entry.reason if entry is not None else None

    def record_success(self, *, provider: str, model: str, base_url: str = "") -> None:
        """The entry answered: its next rate limit starts the ladder from the bottom again."""
        self.strikes.pop(_identity(provider, model, base_url), None)

    def clear(self, *, provider: str, model: str, base_url: str = "") -> None:
        self.entries.pop(_identity(provider, model, base_url), None)
        self._save()

    def clear_reason(self, reason: FailoverReason) -> int:
        """Drop every cooldown armed for ``reason`` (e.g. network ones once connectivity is back)."""
        keys = [k for k, e in self.entries.items() if e.reason is reason]
        for k in keys:
            self.entries.pop(k, None)
        if keys:
            self._save()
        return len(keys)

    def only_network_cooling(self) -> bool:
        """True when at least one entry is cooling and every active cooldown is a network one."""
        now = time.monotonic()
        active = [e for e in self.entries.values() if e.until > now]
        return bool(active) and all(e.reason is FailoverReason.network for e in active)


def _provider_reset_delay(retry_after: Any) -> float | None:
    """Seconds until the provider-declared reset, or None when missing/invalid/negative.

    ``retry_after`` comes from the classifier's Retry-After / body / prose parsing,
    already in seconds-during so wall-clock epoch values never enter the monotonic
    comparison (carried over from Hermes' _provider_reset_delay).
    """
    if retry_after is None:
        return None
    try:
        delay = float(retry_after)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(delay) or delay <= 0:
        return None
    return delay
