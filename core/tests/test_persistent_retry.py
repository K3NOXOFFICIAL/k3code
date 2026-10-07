"""PersistentRetry tests: pause/park/resume with a fake clock (no real network)."""

from __future__ import annotations

from typing import Any

from k3code.errors import AllProvidersUnreachable, ChainExhausted
from k3code.providers.types import Message, StreamEvent, Usage
from k3code.reliability.events import PARKED, PAUSED, RESUMED, UNPARKED, EventEmitter
from k3code.reliability.netwatch import NetState
from k3code.reliability.persistent_retry import (
    CancelToken,
    PersistentRetry,
    RetryConfig,
    TurnCancelled,
)


class FakeClock:
    """monotonic + sleep where sleep advances the clock (no real waiting)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        self.now += delay


class FakeNetWatch:
    def __init__(self, state: NetState) -> None:
        self._state = state

    @property
    def state(self) -> NetState:
        return self._state

    def set(self, state: NetState) -> None:
        self._state = state


class FakeRouter:
    """Fails `fails` times with `error`, then streams a done event."""

    def __init__(self, fails: int, error: Exception, text: str = "done!") -> None:
        self._fails = fails
        self._error = error
        self._text = text
        self.calls = 0
        self.messages_seen: list[Any] = []
        self.on_event = lambda event: None  # non-None so PersistentRetry wraps it

    async def stream(self, messages, tools, **kwargs):
        self.calls += 1
        self.messages_seen.append(list(messages))
        if self.calls <= self._fails:
            raise self._error
        yield StreamEvent(type="text_delta", text=self._text)
        yield StreamEvent(
            type="done",
            message=Message(role="assistant", content=self._text, tool_calls=[]),
            usage=Usage(),
        )


def _retry(router, **kwargs) -> tuple[PersistentRetry, FakeClock, EventEmitter]:
    clock = FakeClock()
    events = EventEmitter()
    seen: list = []
    events.add(lambda e: seen.append(e))
    r = PersistentRetry(
        router,
        kwargs.pop("netwatch", None),
        config=kwargs.pop("config", RetryConfig(park_base=1.0, park_max=8.0, poll_interval=0.5)),
        events=events,
        cancel_token=kwargs.pop("cancel_token", None) or CancelToken(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )
    return r, clock, events, seen


async def _drain(stream):
    return [e async for e in stream]


async def test_offline_pause_resumes_same_turn():
    """OFFLINE -> paused; flip to ONLINE mid-wait -> resumed, same messages retried."""
    nw = FakeNetWatch(NetState.OFFLINE)
    router = FakeRouter(fails=1, error=AllProvidersUnreachable("down", attempts=2))
    retry, clock, events, seen = _retry(router, netwatch=nw)

    orig_sleep = clock.sleep

    async def sleep_and_flip(delay: float):
        nw.set(NetState.ONLINE)  # network recovers while paused
        await orig_sleep(delay)

    retry._sleep = sleep_and_flip

    messages = [Message(role="user", content="hi")]
    out = await _drain(retry.stream(messages, []))

    assert router.calls == 2
    # messages untouched on retry
    assert router.messages_seen[0] == router.messages_seen[1] == messages
    kinds = [e.kind for e in seen]
    assert kinds[0] == PAUSED
    assert kinds[-1] == RESUMED
    assert any(e.type == "text_delta" for e in out)


async def test_captive_pauses_like_offline():
    nw = FakeNetWatch(NetState.CAPTIVE)
    router = FakeRouter(fails=1, error=AllProvidersUnreachable("down"))

    async def sleep_and_flip(delay: float):
        nw.set(NetState.ONLINE)
        clock.now += delay

    retry, clock, events, seen = _retry(router, netwatch=nw)
    retry._sleep = sleep_and_flip
    out = await _drain(retry.stream([Message(role="user", content="x")], []))

    assert router.calls == 2
    assert [e.kind for e in seen] == [PAUSED, RESUMED]
    assert out


async def test_provider_down_parks_with_ladder_then_succeeds():
    """PROVIDER_DOWN is usable-ish -> park ladder 1s, 2s, ... then success."""
    nw = FakeNetWatch(NetState.PROVIDER_DOWN)
    router = FakeRouter(fails=2, error=AllProvidersUnreachable("nope", attempts=3))
    retry, clock, events, seen = _retry(router, netwatch=nw)

    out = await _drain(retry.stream([Message(role="user", content="x")], []))

    assert router.calls == 3
    parked = [e for e in seen if e.kind == PARKED]
    unparked = [e for e in seen if e.kind == UNPARKED]
    assert len(parked) == 2 and len(unparked) == 2
    # exponential ladder: 1.0 then 2.0
    assert parked[0].data["delay"] == 1.0
    assert parked[1].data["delay"] == 2.0
    assert parked[0].data["next_retry_at"] == 1000.0 + 1.0
    assert out


async def test_park_ladder_caps_at_max():
    nw = FakeNetWatch(NetState.ONLINE)
    router = FakeRouter(fails=5, error=AllProvidersUnreachable("nope"))
    retry, clock, events, seen = _retry(router, netwatch=nw)

    await _drain(retry.stream([Message(role="user", content="x")], []))

    parked = [e for e in seen if e.kind == PARKED]
    assert [p.data["delay"] for p in parked] == [1.0, 2.0, 4.0, 8.0, 8.0]  # capped at park_max


async def test_rate_limit_parks_with_retry_after():
    """ChainExhausted(rate_limit) + router.retry Retry-After -> park exactly that long."""

    class RateRouter(FakeRouter):
        async def stream(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1 and callable(self.on_event):
                self.on_event(_RetryEvent(delay=30.0))
                raise ChainExhausted("limited", last_reason="rate_limit")
            yield StreamEvent(
                type="done",
                message=Message(role="assistant", content="ok", tool_calls=[]),
                usage=Usage(),
            )

    router = RateRouter(fails=1, error=ChainExhausted("limited", last_reason="rate_limit"))
    retry, clock, events, seen = _retry(router, netwatch=FakeNetWatch(NetState.ONLINE))

    await _drain(retry.stream([Message(role="user", content="x")], []))

    parked = [e for e in seen if e.kind == PARKED]
    assert len(parked) == 1
    assert parked[0].data["delay"] == 30.0
    assert "retry-after 30s" in parked[0].detail
    assert router.calls == 2


async def test_rate_limit_without_retry_after_uses_ladder():
    router = FakeRouter(fails=1, error=ChainExhausted("limited", last_reason="quota"))
    retry, clock, events, seen = _retry(router, netwatch=FakeNetWatch(NetState.ONLINE))

    await _drain(retry.stream([Message(role="user", content="x")], []))

    parked = [e for e in seen if e.kind == PARKED]
    assert len(parked) == 1
    assert parked[0].data["delay"] == 1.0  # ladder base
    assert parked[0].detail == "quota"


async def test_non_transient_chain_exhausted_propagates():
    router = FakeRouter(fails=99, error=ChainExhausted("bad key", last_reason="auth"))
    retry, clock, events, seen = _retry(router, netwatch=FakeNetWatch(NetState.ONLINE))

    try:
        await _drain(retry.stream([Message(role="user", content="x")], []))
    except ChainExhausted as e:
        assert e.last_reason == "auth"
    else:
        raise AssertionError("ChainExhausted(auth) must propagate")
    assert router.calls == 1
    assert seen == []


async def test_stop_cancels_parked_wait():
    """Cancelled token -> TurnCancelled out of a park."""
    token = CancelToken()
    token.cancel()  # cancel before we even start
    nw = FakeNetWatch(NetState.ONLINE)
    router = FakeRouter(fails=1, error=AllProvidersUnreachable("down"))
    retry, clock, events, seen = _retry(router, netwatch=nw, cancel_token=token)

    try:
        await _drain(retry.stream([Message(role="user", content="x")], []))
    except TurnCancelled:
        pass
    else:
        raise AssertionError("cancelled park must raise TurnCancelled")


async def test_stop_cancels_paused_wait_mid_flight():
    nw = FakeNetWatch(NetState.OFFLINE)
    router = FakeRouter(fails=1, error=AllProvidersUnreachable("down"))
    token = CancelToken()
    retry, clock, events, seen = _retry(router, netwatch=nw, cancel_token=token)

    orig_sleep = clock.sleep

    async def sleep_then_cancel(delay: float):
        token.cancel()  # user hits /stop while paused
        await orig_sleep(delay)

    retry._sleep = sleep_then_cancel

    try:
        await _drain(retry.stream([Message(role="user", content="x")], []))
    except TurnCancelled:
        pass
    else:
        raise AssertionError("cancelled pause must raise TurnCancelled")
    assert router.calls == 1  # never retried


async def test_max_wait_reraises_original():
    """max_wait hit -> the original AllProvidersUnreachable surfaces."""
    nw = FakeNetWatch(NetState.OFFLINE)  # never recovers
    router = FakeRouter(fails=99, error=AllProvidersUnreachable("down", attempts=1))
    retry, clock, events, seen = _retry(
        router,
        netwatch=nw,
        config=RetryConfig(park_base=1.0, park_max=8.0, poll_interval=0.5, max_wait=2.0),
    )

    try:
        await _drain(retry.stream([Message(role="user", content="x")], []))
    except AllProvidersUnreachable:
        pass
    else:
        raise AssertionError("max_wait must surface the original error")
    paused = [e for e in seen if e.kind == PAUSED]
    assert len(paused) == 1
    assert clock.now >= 1002.0  # fake clock advanced to the deadline


async def test_no_netwatch_parks_on_unreachable():
    router = FakeRouter(fails=1, error=AllProvidersUnreachable("down"))
    retry, clock, events, seen = _retry(router, netwatch=None)

    await _drain(retry.stream([Message(role="user", content="x")], []))

    assert router.calls == 2
    assert [e.kind for e in seen] == [PARKED, UNPARKED]


class _RetryEvent:
    """Minimal router.retry event shape (attribute access)."""

    def __init__(self, delay: float) -> None:
        self.kind = "router.retry"
        self.reason = "rate_limit"
        self.extra = {"delay": delay}
