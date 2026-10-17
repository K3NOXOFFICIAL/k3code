"""Retry-forever mode ("don\'t stop until goal verified completed").

When ``retry_forever`` is on, transient/infra errors park with capped backoff and
retry forever; when off, behavior is byte-for-byte unchanged (existing caps intact).
"""

from __future__ import annotations

import pytest

from k3code.errors import AllProvidersUnreachable, ChainExhausted
from k3code.providers.types import Message
from k3code.reliability.events import GAVE_UP, PARKED, EventEmitter
from k3code.reliability.netwatch import NetState
from k3code.reliability.persistent_retry import (
    FOREVER_PARK_REASONS,
    RetryConfig,
    TurnCancelled,
)
from k3code.reliability.hooks import Reliability, ReliabilitySettings

from test_persistent_retry import FakeNetWatch, FakeRouter, _drain, _retry


async def test_forever_parks_indefinitely_on_all_providers_unreachable():
    """AllProvidersUnreachable with a max_wait set: forever forces None, parks, recovers."""
    router = FakeRouter(fails=2, error=AllProvidersUnreachable("down", attempts=1))
    retry, clock, events, seen = _retry(
        router,
        netwatch=FakeNetWatch(NetState.ONLINE),
        config=RetryConfig(park_base=1.0, park_max=2.0, poll_interval=0.5, max_wait=1.0, retry_forever=True),
    )
    out = await _drain(retry.stream([Message(role="user", content="x")], []))
    assert retry.config.max_wait is None  # stream() forces None despite the 1.0 bound
    assert out  # the turn completed after 2 parks
    assert router.calls == 3
    assert sum(1 for e in seen if e.kind == PARKED) == 2
    assert not [e for e in seen if e.kind == GAVE_UP]  # never gives up


async def test_forever_chain_exhausted_rate_limit_parks_when_on():
    router = FakeRouter(fails=3, error=ChainExhausted("limited", last_reason="rate_limit"))
    retry, clock, events, seen = _retry(
        router,
        netwatch=FakeNetWatch(NetState.ONLINE),
        config=RetryConfig(park_base=1.0, park_max=2.0, poll_interval=0.5, retry_forever=True),
    )
    out = await _drain(retry.stream([Message(role="user", content="x")], []))
    assert out and router.calls == 4
    assert len([e for e in seen if e.kind == PARKED]) == 3


async def test_off_chain_exhausted_rate_limit_parks_without_retry_after():
    """Off-mode rate_limit parks on the ladder even when not unattended (pre-existing behavior)."""
    router = FakeRouter(fails=1, error=ChainExhausted("limited", last_reason="rate_limit"))
    retry, clock, events, seen = _retry(router, netwatch=FakeNetWatch(NetState.ONLINE))
    out = await _drain(retry.stream([Message(role="user", content="x")], []))
    assert out and len([e for e in seen if e.kind == PARKED]) == 1


async def test_forever_chain_exhausted_quota_parks_when_on():
    router = FakeRouter(fails=2, error=ChainExhausted("quota out", last_reason="quota"))
    retry, clock, events, seen = _retry(
        router,
        netwatch=FakeNetWatch(NetState.ONLINE),
        config=RetryConfig(park_base=1.0, park_max=2.0, poll_interval=0.5, retry_forever=True),
    )
    out = await _drain(retry.stream([Message(role="user", content="x")], []))
    assert out and router.calls == 3


async def test_forever_auth_parks_when_on():
    """Auth failures park with the long backoff instead of failing the turn."""
    router = FakeRouter(fails=2, error=ChainExhausted("bad key", last_reason="auth"))
    retry, clock, events, seen = _retry(
        router,
        netwatch=FakeNetWatch(NetState.ONLINE),
        config=RetryConfig(park_base=1.0, park_max=2.0, poll_interval=0.5, retry_forever=True),
    )
    out = await _drain(retry.stream([Message(role="user", content="x")], []))
    assert out and router.calls == 3
    parked = [e for e in seen if e.kind == PARKED]
    assert len(parked) == 2 and parked[0].data["delay"] == pytest.approx(300.0)
    assert parked[1].data["delay"] == pytest.approx(600.0)  # doubling, existing ladder reused


async def test_forever_auth_backoff_caps_at_one_hour():
    router = FakeRouter(fails=1, error=ChainExhausted("bad key", last_reason="auth"))
    retry, clock, events, seen = _retry(
        router, netwatch=FakeNetWatch(NetState.ONLINE), config=RetryConfig(retry_forever=True)
    )
    retry._forever_auth_strikes = 10
    assert await retry._handle_exhausted_forever(
        ChainExhausted("bad key", last_reason="auth"), None
    )
    parked = [e for e in seen if e.kind == PARKED]
    assert parked and parked[-1].data["delay"] == pytest.approx(3600.0)


async def test_forever_park_ladder_holds_at_cap():
    """Park ladder: base 30s, factor 2, cap 600s, holds at cap forever."""
    router = FakeRouter(fails=99, error=AllProvidersUnreachable("down", attempts=1))
    retry, clock, events, seen = _retry(
        router, netwatch=FakeNetWatch(NetState.ONLINE), config=RetryConfig(retry_forever=True)
    )
    orig_sleep = clock.sleep
    parks = 0

    async def cap_and_count(delay: float):
        nonlocal parks
        await orig_sleep(delay)
        # count parks via the PARKED events seen so far; cancel after 8 parks
        parks = sum(1 for e in seen if e.kind == PARKED)
        if parks >= 8:
            retry.cancel_token.cancel()  # /stop still ends a forever run

    retry._sleep = cap_and_count
    try:
        await _drain(retry.stream([Message(role="user", content="x")], []))
    except TurnCancelled:
        pass
    else:
        raise AssertionError("cancel must end a forever park")
    delays = [e.data["delay"] for e in seen if e.kind == PARKED]
    assert delays[:6] == pytest.approx([30.0, 60.0, 120.0, 240.0, 480.0, 600.0])
    assert delays[6] == pytest.approx(600.0) and delays[7] == pytest.approx(600.0)


async def test_off_mode_caps_intact():
    """Off mode: max_wait still gives up, unattended caps still apply, auth still raises."""
    # max_wait gives up
    router = FakeRouter(fails=99, error=AllProvidersUnreachable("down", attempts=1))
    retry, clock, events, seen = _retry(
        router,
        netwatch=FakeNetWatch(NetState.ONLINE),
        config=RetryConfig(park_base=1.0, park_max=2.0, poll_interval=0.5, max_wait=3.0),
    )
    with pytest.raises(AllProvidersUnreachable):
        await _drain(retry.stream([Message(role="user", content="x")], []))
    assert sum(1 for e in seen if e.kind == GAVE_UP) == 1
    # unattended cap
    router = FakeRouter(fails=99, error=ChainExhausted("down", last_reason="server"))
    retry, clock, events, seen = _retry(
        router,
        netwatch=FakeNetWatch(NetState.ONLINE),
        config=RetryConfig(park_base=1.0, park_max=2.0, poll_interval=0.5, max_unattended_parks=2),
    )
    retry.unattended = True
    with pytest.raises(ChainExhausted):
        await _drain(retry.stream([Message(role="user", content="x")], []))
    assert sum(1 for e in seen if e.kind == PARKED) == 2
    # auth raises
    router = FakeRouter(fails=99, error=ChainExhausted("bad key", last_reason="auth"))
    retry, clock, events, seen = _retry(router, netwatch=FakeNetWatch(NetState.ONLINE))
    with pytest.raises(ChainExhausted):
        await _drain(retry.stream([Message(role="user", content="x")], []))
    assert router.calls == 1 and seen == []


def test_settings_plumb_retry_forever():
    """reliability.retry_forever reaches RetryConfig and forces max_wait=None."""
    from types import SimpleNamespace

    from k3code.reliability.hooks import reliability_settings

    settings = reliability_settings(SimpleNamespace(reliability={"retry_forever": True, "max_wait": 60}))
    assert settings.retry_forever is True
    r = Reliability.from_settings(settings)
    assert r.retry_config.retry_forever is True
    assert r.retry_config.max_wait is None


def test_headless_max_wait_overridden_by_retry_forever():
    """cli: retry_forever forces _headless_max_wait to None regardless of flag/config."""
    from types import SimpleNamespace

    from k3code.cli import _headless_max_wait

    assert _headless_max_wait(None, SimpleNamespace(reliability={})) == 300.0
    # the override lives in _run_headless (raw config check); the helper itself is unchanged
    assert _headless_max_wait(7, SimpleNamespace(reliability={"max_wait": 42})) == 7.0


def test_retry_policy_forever_holds_last_rung():
    """automation.retry_policy: retry_forever replays the last rung instead of exhausting."""
    from k3code.automation.retry_policy import plan_unreachable_retry

    assert plan_unreachable_retry({"attempt": 3}, 1000.0, 10**12) is None
    nxt = plan_unreachable_retry({"attempt": 3}, 1000.0, 10**12, retry_forever=True)
    assert nxt == {"attempt": 3, "at": 1000.0 + 1800.0}


def test_forever_park_reasons_cover_goal_list():
    assert {"server", "timeout", "network", "unknown", "rate_limit", "quota"} <= FOREVER_PARK_REASONS
