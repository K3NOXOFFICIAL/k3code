"""Issue #108: the retry wrapper says when it stops waiting, so a headless run can report it and exit 75."""

from __future__ import annotations

import pytest

from k3code.errors import AllProvidersUnreachable
from k3code.providers.types import Message
from k3code.reliability.events import GAVE_UP, PAUSED, EventEmitter
from k3code.reliability.netwatch import NetState
from k3code.reliability.persistent_retry import PersistentRetry, RetryConfig


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class _OfflineNetWatch:
    state = NetState.OFFLINE


class _DownRouter:
    on_event = staticmethod(lambda event: None)

    async def stream(self, messages, tools, **kwargs):
        raise AllProvidersUnreachable("down", attempts=1)
        yield  # pragma: no cover - makes this an async generator


async def test_max_wait_emits_gave_up_before_the_original_error_surfaces():
    clock, events, seen = _Clock(), EventEmitter(), []
    events.add(seen.append)
    retry = PersistentRetry(
        _DownRouter(),
        _OfflineNetWatch(),
        config=RetryConfig(park_base=1.0, park_max=8.0, poll_interval=0.5, max_wait=2.0),
        events=events,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(AllProvidersUnreachable):
        async for _ in retry.stream([Message(role="user", content="x")], []):
            pass

    kinds = [e.kind for e in seen]
    assert kinds.count(PAUSED) == 1 and kinds.count(GAVE_UP) == 1
    gave_up = next(e for e in seen if e.kind == GAVE_UP)
    assert gave_up.data["waited"] == 2.0 and "2s" in gave_up.detail
