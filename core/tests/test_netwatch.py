"""NetWatch state-machine tests with fake probes (no real network)."""

from __future__ import annotations

import asyncio

import pytest

from k3code.reliability.netwatch import (
    InternetProbeResult,
    NetState,
    NetWatch,
    NetWatchConfig,
    ProviderProbeState,
    _nm_verdict,
)


def _config(**overrides) -> NetWatchConfig:
    return NetWatchConfig(**overrides)


def _internet(result: InternetProbeResult):
    async def probe(config):
        return result

    return probe


async def _never_probe(base_url: str, timeout: float):
    raise AssertionError("provider probe must not be called")


async def _nm_probe() -> None:
    return None


def _watch(**kwargs) -> NetWatch:
    kwargs.setdefault("internet_probe", _internet(InternetProbeResult(ok=True, latency_ms=10.0)))
    kwargs.setdefault("provider_probe", _never_probe)
    kwargs.setdefault("nm_probe", _nm_probe)
    return NetWatch(**kwargs)


async def test_offline_to_online_transition():
    """Internet down -> OFFLINE; back up -> ONLINE, with callback notifications."""
    first = InternetProbeResult(ok=False)
    second = InternetProbeResult(ok=True, latency_ms=10.0)
    calls = {"n": 0}

    async def flap(config):
        calls["n"] += 1
        return first if calls["n"] == 1 else second

    nw = NetWatch(internet_probe=flap, provider_probe=_never_probe, nm_probe=_nm_probe)
    seen: list[tuple[NetState, NetState]] = []
    nw.subscribe(lambda old, new: seen.append((old, new)))

    await nw._probe_all()
    assert nw.state is NetState.OFFLINE
    assert seen == [(NetState.ONLINE, NetState.OFFLINE)]

    await nw._probe_all()
    assert nw.state is NetState.ONLINE
    assert seen[-1] == (NetState.OFFLINE, NetState.ONLINE)


async def test_flapping_internet_settles_online():
    """Intermittent ok/fail probes must not leave a stale state."""
    results = [InternetProbeResult(ok=False), InternetProbeResult(ok=True, latency_ms=5.0)]
    it = iter(results * 2)

    async def flap(config):
        return next(it)

    nw = NetWatch(internet_probe=flap, provider_probe=_never_probe, nm_probe=_nm_probe)
    for _ in range(4):
        await nw._probe_all()
    assert nw.state is NetState.ONLINE


async def test_degraded_on_high_latency():
    nw = _watch(internet_probe=_internet(InternetProbeResult(ok=True, latency_ms=5000.0)))
    await nw._probe_all()
    assert nw.state is NetState.DEGRADED


async def test_captive_portal():
    nw = _watch(internet_probe=_internet(InternetProbeResult(ok=False, latency_ms=30.0, captive=True)))
    await nw._probe_all()
    assert nw.state is NetState.CAPTIVE


async def test_provider_down_is_not_offline():
    """Internet fine + all providers failing -> PROVIDER_DOWN (fail over, not pause)."""

    async def provider_down(base_url: str, timeout: float):
        raise ConnectionError("boom")

    nw = NetWatch(
        _config(provider_fail_threshold=1),
        internet_probe=_internet(InternetProbeResult(ok=True, latency_ms=10.0)),
        provider_probe=provider_down,
        nm_probe=_nm_probe,
    )
    nw.add_provider("p1", "http://127.0.0.1:9/v1")
    await nw._probe_all()

    assert nw.provider_state("p1") is NetState.PROVIDER_DOWN
    assert nw.all_providers_down()
    assert nw.state is NetState.PROVIDER_DOWN
    assert nw.state.usable_for_llm  # fail-over signal, not a pause signal


async def test_single_provider_down_does_not_sink_state():
    """One healthy provider keeps the global state usable."""

    async def provider(base_url: str, timeout: float):
        if "good" in base_url:
            return (200, "{}", base_url)
        raise ConnectionError("down")

    nw = NetWatch(
        _config(provider_fail_threshold=1),
        internet_probe=_internet(InternetProbeResult(ok=True, latency_ms=10.0)),
        provider_probe=provider,
        nm_probe=_nm_probe,
    )
    nw.add_provider("good", "http://good.test/v1")
    nw.add_provider("bad", "http://bad.test/v1")
    await nw._probe_all()

    assert not nw.all_providers_down()
    assert nw.state is NetState.ONLINE


async def test_all_providers_down_false_without_providers():
    nw = _watch()
    assert nw.all_providers_down() is False
    await nw._probe_all()
    assert nw.state is NetState.ONLINE


async def test_provider_needs_threshold_consecutive_fails():
    """A single blip below provider_fail_threshold must not mark PROVIDER_DOWN."""
    calls = {"n": 0}

    async def flaky(base_url: str, timeout: float):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("blip")
        return (200, "{}", base_url)

    nw = NetWatch(
        _config(provider_fail_threshold=3),
        internet_probe=_internet(InternetProbeResult(ok=True, latency_ms=10.0)),
        provider_probe=flaky,
        nm_probe=_nm_probe,
    )
    nw.add_provider("p", "http://p.test/v1")
    await nw._probe_all()
    assert nw.provider_state("p") is NetState.ONLINE
    assert nw.state is NetState.ONLINE


async def test_5xx_counts_as_fail_2xx_as_ok():
    seq = [(503, "", "u"), (200, "{}", "u")]
    it = iter(seq)

    async def probe(base_url: str, timeout: float):
        return next(it)

    nw = NetWatch(
        _config(provider_fail_threshold=2),
        internet_probe=_internet(InternetProbeResult(ok=True, latency_ms=10.0)),
        provider_probe=probe,
        nm_probe=_nm_probe,
    )
    nw.add_provider("p", "http://p.test/v1")
    await nw._probe_all()
    assert nw.provider_state("p") is NetState.ONLINE  # only 1 fail < threshold
    await nw._probe_all()
    assert nw.provider_state("p") is NetState.ONLINE  # recovered


async def test_subscribe_sync_and_async():
    nw = _watch(internet_probe=_internet(InternetProbeResult(ok=False)))
    sync_seen: list = []
    async_seen: list = []

    async def async_cb(old, new):
        async_seen.append((old, new))

    def bad_cb(old, new):
        raise RuntimeError("subscriber must not break the probe")

    nw.subscribe(lambda old, new: sync_seen.append((old, new)))
    nw.subscribe(async_cb)
    nw.subscribe(bad_cb)
    await nw._probe_all()

    assert sync_seen == [(NetState.ONLINE, NetState.OFFLINE)]
    assert async_seen == [(NetState.ONLINE, NetState.OFFLINE)]
    assert nw.state is NetState.OFFLINE


async def test_wait_until_usable_named_provider_up():
    """A named provider that is up returns immediately without sleeping."""
    slept: list[float] = []

    async def sleep(d: float):
        slept.append(d)

    nw = NetWatch(
        internet_probe=_internet(InternetProbeResult(ok=True, latency_ms=10.0)),
        provider_probe=_never_probe,
        nm_probe=_nm_probe,
        sleep=sleep,
    )
    nw._providers["p"] = ProviderProbeState(name="p", base_url="http://x.test", state=NetState.ONLINE)
    st = await nw.wait_until_usable("p")
    assert st.usable_for_llm
    assert slept == []


async def test_wait_until_usable_waits_while_offline_then_returns():
    """OFFLINE -> no return until the state flips to usable."""
    slept = 0

    async def sleep(d: float):
        nonlocal slept
        slept += 1
        if slept >= 2:
            nw._state = NetState.ONLINE

    nw = NetWatch(
        internet_probe=_internet(InternetProbeResult(ok=False)),
        provider_probe=_never_probe,
        nm_probe=_nm_probe,
        sleep=sleep,
    )
    nw._state = NetState.OFFLINE
    st = await nw.wait_until_usable()
    assert st is NetState.ONLINE
    assert slept >= 1


def test_usable_for_llm_matrix():
    assert NetState.ONLINE.usable_for_llm
    assert NetState.DEGRADED.usable_for_llm
    assert NetState.PROVIDER_DOWN.usable_for_llm
    assert not NetState.OFFLINE.usable_for_llm
    assert not NetState.CAPTIVE.usable_for_llm


def _provider_state_online(name: str):  # kept for clarity in failure messages
    return ProviderProbeState(name=name, base_url="http://x.test", state=NetState.ONLINE)


def test_nm_verdict_mapping():
    assert _nm_verdict(None) == "unknown"
    assert _nm_verdict("disconnected") == "bad"
    assert _nm_verdict("asleep") == "bad"
    assert _nm_verdict("connecting") == "bad"
    assert _nm_verdict("portal") == "portal"
    assert _nm_verdict("limited") == "portal"
    assert _nm_verdict("connected") == "ok"


async def test_network_manager_reconnect_kicks_a_probe():
    """NM going bad→ok never re-probed (only ok→bad did), and the NM loop shared the monitor's wakeup event, so it
    could swallow a kick: after an outage the state stayed offline for up to max_interval."""
    import asyncio

    nm, online, probes = ["disconnected"], [False], []

    async def internet(config):
        probes.append(nm[0])
        return InternetProbeResult(ok=online[0], latency_ms=10.0)

    async def nm_probe():
        return nm[0]

    cfg = _config(base_interval=30, max_interval=60, nm_poll_interval=0.05)
    nw = _watch(config=cfg, internet_probe=internet, nm_probe=nm_probe)
    await nw.start()
    try:
        for _ in range(100):
            if nw.state == NetState.OFFLINE:
                break
            await asyncio.sleep(0.02)
        assert nw.state == NetState.OFFLINE
        nm[0], online[0] = "connected", True
        for _ in range(100):
            if nw.state == NetState.ONLINE:
                break
            await asyncio.sleep(0.02)
        assert nw.state == NetState.ONLINE and "connected" in probes
    finally:
        await nw.stop()


async def test_start_stop_lifecycle():
    nw = _watch()
    await nw.start()
    await nw.stop()
    # start again works (idempotent-ish, fresh tasks)
    await nw.start()
    await nw.stop()


async def test_rearming_a_bundle_subscribes_the_net_state_forwarder_once():
    """start() ran _forward_net_states() again after every stop(): each idle-sweep re-arm stacked one more
    subscriber, so one connectivity change was delivered N times to the session's clients."""
    from k3code.reliability import Reliability
    from k3code.reliability import events as ev

    rel = Reliability.from_settings(None, session="s")
    assert rel.netwatch is not None
    delivered = []
    rel.events.add(lambda e: delivered.append(e) if e.kind == ev.NET_STATE else None)
    for _ in range(10):
        await rel.start()
        await rel.stop()
    for cb in list(rel.netwatch._callbacks):
        cb(NetState.ONLINE, NetState.OFFLINE)
    assert len(delivered) == 1


@pytest.mark.parametrize("swallow", ["error", "return"])
@pytest.mark.parametrize("loop", ["monitor", "nm"])
async def test_loops_end_when_a_probe_swallows_the_cancel(loop, swallow):
    """Issue #42: httpcore/anyio can lose a task.cancel() inside a shielded checkpoint, so the probe comes back with an
    ordinary error (or a plain result) instead of CancelledError. The loops caught that as "just a probe error" and ran
    on, and the event-loop teardown of the test (Runner.close -> _cancel_all_tasks) then waited for them for ever."""
    entered = asyncio.Event()

    async def swallowing_probe(*_args):
        entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            if swallow == "error":
                raise OSError("proxy refused") from None  # what a lost cancel looks like from outside
            return InternetProbeResult(ok=True, latency_ms=1.0) if loop == "monitor" else None

    probes = {"internet_probe": swallowing_probe} if loop == "monitor" else {"nm_probe": swallowing_probe}
    nw = _watch(config=_config(base_interval=0.01, nm_poll_interval=0.01), **probes)
    await nw.start()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for t in nw._tasks:  # what asyncio.Runner.close does: cancel from outside, never via stop()
            t.cancel()
        _done, pending = await asyncio.wait(nw._tasks, timeout=3)
        assert not pending, "a monitor task kept running after it was cancelled"
    finally:
        await nw.stop()
