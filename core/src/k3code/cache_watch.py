"""Notices when the provider's prompt cache stops hitting.

A cache miss re-sends and re-bills the whole prompt, so a prompt that changes between calls (a timestamp, a reordered
section, a tool list that shifts) is the most expensive silent bug an agent loop has. The gateway already reports
``cache_hit_pct`` per call; this watches it over a session and says so once when it collapses.
"""

from __future__ import annotations

import statistics
from collections import deque

#: A hit rate this many percentage points under the recent median is a regression.
DROP_POINTS = 40.0
#: How many recent calls make the baseline, and how many it needs before it means anything.
WINDOW = 5
MIN_SAMPLES = 2
#: A pause this long lets the provider's cache expire by design (the 5 minute TTL): the first call after it is a
#: miss that says nothing about the prompt.
IDLE_S = 300.0
#: Low calls in a row before it counts. A failover to another entry, a tier escalation, a batch of elided tool results
#: or a fresh claude-cli process each cost one miss and hit again on the next call; a prompt that really changes
#: between calls misses on every one.
CONFIRM = 2


class CacheWatch:
    """One warning per episode: the rate stays ``drop_points`` under its recent median for ``CONFIRM`` calls in a row,
    stays there without further notice, and arms again once it climbs back. The baseline is frozen during an episode
    (and by the low calls before it), so the low calls do not become the new normal."""

    def __init__(self, *, drop_points: float = DROP_POINTS, idle_s: float = IDLE_S) -> None:
        self.drop_points = drop_points
        self.idle_s = idle_s
        self._recent: deque[float] = deque(maxlen=WINDOW)
        self._in_episode = False
        self._low_run = 0
        self._baseline = 0.0
        self._last_at: float | None = None

    def reset(self) -> None:
        """The prompt prefix changed on purpose (compaction, /clear, a model switch): start over."""
        self._recent.clear()
        self._in_episode = False
        self._low_run = 0
        self._last_at = None

    def observe(self, prompt_tokens: int, cache_read_tokens: int, now: float) -> str | None:
        """Take in one call's usage; the warning text when this call starts a regression, else None."""
        idle = self._last_at is not None and now - self._last_at > self.idle_s
        self._last_at = now
        if prompt_tokens <= 0:
            return None
        if idle:  # the cache expired by itself: neither a regression nor a baseline
            self.reset()
            self._last_at = now
            return None
        hit = 100.0 * min(cache_read_tokens, prompt_tokens) / prompt_tokens
        if self._in_episode:
            if hit >= self._baseline - self.drop_points / 2:  # back near where it was
                self._in_episode = False
                self._recent.append(hit)
            return None
        if len(self._recent) >= MIN_SAMPLES:
            baseline = statistics.median(self._recent)
            if baseline - hit >= self.drop_points:
                self._low_run += 1
                if self._low_run < CONFIRM:
                    return None  # one miss: wait for the next call before saying anything
                self._in_episode = True
                self._baseline = baseline
                return (
                    f"The prompt cache stopped hitting: {hit:.0f}% of this request was served from it, against "
                    f"about {baseline:.0f}% before, and the call before it missed too. Something in the prompt "
                    "changes between calls, so each call now pays for the whole context again."
                )
        self._low_run = 0
        self._recent.append(hit)
        return None
