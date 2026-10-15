"""Issue #109: an idle daemon probed the internet and every provider every 5 s; healthy polling is now 60 s, and the
gateway/daemon no longer log httpx's line per request."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

from k3code.reliability.netwatch import InternetProbeResult, NetState, NetWatch, NetWatchConfig


async def _nm_probe() -> None:
    return None


async def _no_provider(base_url: str, timeout: float):
    raise AssertionError("provider probe must not be called")


def _watch(ok: bool) -> NetWatch:
    async def internet(config):
        return InternetProbeResult(ok=ok, latency_ms=10.0)

    return NetWatch(
        config=NetWatchConfig(base_interval=5.0, max_interval=60.0, healthy_interval=60.0),
        internet_probe=internet,
        provider_probe=_no_provider,
        nm_probe=_nm_probe,
    )


def test_healthy_polling_is_slow():
    nw = _watch(ok=True)  # the state starts ONLINE
    assert nw._next_interval(was_usable=True) == 60.0


async def test_an_outage_is_watched_closely_first_then_backs_off():
    nw = _watch(ok=False)
    await nw._probe_all()
    assert nw.state is NetState.OFFLINE
    assert nw._next_interval(was_usable=True) == 5.0  # the usable -> unusable transition resets the ladder
    nw._interval = 5.0
    assert nw._next_interval(was_usable=False) == 10.0
    nw._interval = 40.0
    assert nw._next_interval(was_usable=False) == 60.0


async def test_the_monitor_loop_settles_on_the_healthy_interval():
    nw = _watch(ok=True)
    await nw.start()
    try:
        await nw.re_probe()
        for _ in range(200):
            if nw.next_interval() == 60.0:
                break
            await asyncio.sleep(0.01)
        assert nw.next_interval() == 60.0
    finally:
        await nw.stop()


def test_service_logging_keeps_httpx_quiet():
    env = {k: v for k, v in os.environ.items() if k != "K3CODE_LOG_LEVEL"}
    code = (
        "import logging; from k3code.cli import _configure_service_logging; _configure_service_logging(); "
        "print(logging.getLevelName(logging.getLogger('httpx').level))"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == logging.getLevelName(logging.WARNING)
