# Ported (design) from hermes-agent cron/unreachable_retry.py and cron/quota_hold.py (MIT); first-party code.
"""Reliability rules for scheduled runs that never reached the model.

* Unreachable ladder: a run that failed with a network error and made zero API calls is re-run after 5, 15 and 30
  minutes (nothing executed, nothing spent, so a re-run cannot double a side effect). Any run that reaches the
  model resets the ladder.
* Quota hold: a provider that says "retry after N s" parks the job at that boundary (+ slack).
"""

from __future__ import annotations

import re
from typing import Any

RETRY_DELAYS_S: tuple[float, ...] = (300.0, 900.0, 1800.0)
HOLD_SLACK_S = 60.0

_RETRY_AFTER_RE = re.compile(r"retry[- ]after[:= ]+(\d+(?:\.\d+)?)\s*s?", re.I)
_QUOTA_RE = re.compile(r"quota|rate.?limit|usage limit|429|too many requests", re.I)
_NETWORK_RE = re.compile(
    r"unreachable|connection (?:error|refused|reset|failed)|timed? ?out|network|dns|name resolution|offline|"
    r"temporary failure|no route|connect(?:ion)? error",
    re.I,
)


def classify_failure(error: str, exc: BaseException | None = None) -> tuple[str, float | None]:
    """→ (``unreachable`` | ``quota`` | ``other``, provider-stated wait seconds)."""
    from k3code.errors import AllProvidersUnreachable

    text = error or ""
    if isinstance(exc, AllProvidersUnreachable):
        return "unreachable", None
    m = _RETRY_AFTER_RE.search(text)
    if _QUOTA_RE.search(text) or (m and "retry after" in text.lower()):
        return "quota", float(m.group(1)) if m else None
    if _NETWORK_RE.search(text):
        return "unreachable", None
    return "other", None


def plan_unreachable_retry(state: dict[str, Any] | None, now: float, natural_next: float) -> dict[str, Any] | None:
    """Next ladder rung as ``{"attempt", "at"}``, or None (exhausted, or the natural occurrence comes first)."""
    attempt = int((state or {}).get("attempt") or 0)
    if attempt >= len(RETRY_DELAYS_S):
        return None
    at = now + RETRY_DELAYS_S[attempt]
    if natural_next <= at:
        return None
    return {"attempt": attempt + 1, "at": at}


def quota_boundary(now: float, retry_after: float) -> float:
    return now + retry_after + HOLD_SLACK_S
