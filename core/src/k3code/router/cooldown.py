# Vendored from hermes-agent@4127d78da84b1eee105f298979cc57cc7457f98d:agent/fallback_cooldown.py (MIT)
# Copyright (c) 2025 Nous Research
# Adapted for k3code: Hermes arms cooldowns on the agent object; k3code keeps a
# standalone CooldownStore keyed by (provider, model, base_url), so the router can
# consult it without an agent. Retry-After / provider reset windows are honored
# through the vendored retry_utils parsers.

"""Per-entry rate-limit cooldowns for the fallback walk.

A failing entry is put into cooldown until the provider's reset window (Retry-After,
body reset fields or free-text reset grammars) — or an exponential floor when the
provider declares nothing. The router skips entries still in cooldown.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from k3code.router.classifier import FailoverReason

_COOLDOWN_REASONS = frozenset({FailoverReason.rate_limit, FailoverReason.quota})

# Exponential fallback when the provider declares no reset: 60 s → 2 m → 4 m … capped.
_BASE_COOLDOWN_SECONDS = 60.0
_MAX_COOLDOWN_SECONDS = 14400.0  # 4 h, carried over from Hermes


def _identity(provider: str, model: str, base_url: str) -> tuple[str, str, str]:
    return (provider.strip().lower(), model.strip().lower(), (base_url or "").strip().lower())


@dataclass
class EntryCooldown:
    """One entry's cooldown window on the monotonic clock."""

    until: float
    seconds: float
    reason: FailoverReason

    @property
    def remaining(self) -> float:
        return max(0.0, self.until - time.monotonic())


@dataclass
class CooldownStore:
    """Cooldowns keyed by (provider, model, base_url); a probe-safe stand-in for
    Hermes' agent-object fields."""

    entries: dict[tuple[str, str, str], EntryCooldown] = field(default_factory=dict)

    def arm(
        self,
        reason: FailoverReason,
        *,
        provider: str,
        model: str,
        base_url: str = "",
        retry_after: float | None = None,
        backoff_count: int = 0,
        now: float | None = None,
    ) -> float | None:
        """Put an entry into cooldown until its reset window. Returns the armed seconds."""
        if reason not in _COOLDOWN_REASONS:
            return None
        if provider_delta := _provider_reset_delay(retry_after):
            seconds = math.ceil(provider_delta)
        else:
            seconds = min(_BASE_COOLDOWN_SECONDS * (2**backoff_count), _MAX_COOLDOWN_SECONDS)
        monotonic_now = time.monotonic() if now is None else now
        self.entries[_identity(provider, model, base_url)] = EntryCooldown(
            until=monotonic_now + seconds,
            seconds=seconds,
            reason=reason,
        )
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

    def clear(self, *, provider: str, model: str, base_url: str = "") -> None:
        self.entries.pop(_identity(provider, model, base_url), None)


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
