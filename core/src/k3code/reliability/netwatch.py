"""Network connectivity state machine for offline pause/resume.

States and their meaning (M2 task spec):

- ``ONLINE``: internet and all registered providers respond normally.
- ``DEGRADED``: internet responds but slowly (or the generic probe was flaky).
- ``PROVIDER_DOWN``: the internet works, but registered provider endpoints do
  not. One provider being down does NOT mean offline — it means "fail over".
- ``CAPTIVE``: an HTTP probe was redirected to a captive portal page.
- ``OFFLINE``: no usable internet connectivity.

Signals: NetworkManager over ``nmcli`` (subprocess, degrade gracefully when
absent), a per-endpoint HTTP probe per registered provider base URL, and a
generic internet probe (HTTP 204 endpoint + TCP connect fallback), both
configurable. Polling uses backoff while unhealthy (5 s up to 60 s); a change
reported by NetworkManager triggers an immediate re-probe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class NetState(Enum):
    """Network connectivity states."""

    ONLINE = "online"
    DEGRADED = "degraded"
    PROVIDER_DOWN = "provider_down"
    CAPTIVE = "captive"
    OFFLINE = "offline"

    @property
    def usable_for_llm(self) -> bool:
        """Whether provider traffic may sensibly go out in this state."""
        return self in (NetState.ONLINE, NetState.DEGRADED, NetState.PROVIDER_DOWN)


@dataclass
class ProviderProbeState:
    """Per-provider endpoint probe bookkeeping."""

    name: str
    base_url: str
    state: NetState = NetState.ONLINE
    last_ok: float = 0.0
    last_fail: float = 0.0
    consecutive_fails: int = 0


@dataclass
class NetWatchConfig:
    """Knobs for the probes and the polling loop."""

    # Generic internet probes
    http_probe_url: str = "https://www.gstatic.com/generate_204"
    tcp_probe_host: str = "1.1.1.1"
    tcp_probe_port: int = 443

    http_timeout: float = 5.0
    tcp_timeout: float = 3.0

    # Polling: 5 s base, rising to 60 s while unhealthy.
    base_interval: float = 5.0
    max_interval: float = 60.0
    backoff_factor: float = 2.0

    # Per-provider probe: HEAD/GET against base_url + path with a short timeout.
    provider_probe_path: str = "/v1/models"
    provider_probe_timeout: float = 5.0
    provider_fail_threshold: int = 3  # consecutive fails → PROVIDER_DOWN

    # Latency above which the generic probe counts as DEGRADED.
    degraded_threshold_ms: float = 2000.0

    # NetworkManager polling (nmcli subprocess, lowest-cost signal source).
    nm_poll_interval: float = 2.0


# ── Probe secrets (injected; tests replace these with fakes) ─────────────


class InternetProbeResult:
    """Result of the generic internet probe."""

    __slots__ = ("ok", "latency_ms", "captive")

    def __init__(self, ok: bool, latency_ms: float | None = None, captive: bool = False) -> None:
        self.ok = ok
        self.latency_ms = latency_ms
        self.captive = captive


class HTTPProbe(Protocol):
    async def __call__(self, url: str, timeout: float) -> tuple[int, str, str]: ...


class ProviderHTTPProbe(Protocol):
    """Returns (status_code, body, final_url) or raises on transport failure."""

    async def __call__(self, base_url: str, timeout: float) -> tuple[int, str, str]: ...


ProbeHTTPStatus = tuple[int, str, str]


async def http_probe(url: str, timeout: float) -> ProbeHTTPStatus:
    """Default HTTP probe: GET with redirects followed, returns (status, body, final_url)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
    return resp.status_code, resp.text, str(resp.url)


async def _tcp_probe(host: str, port: int, timeout: float) -> None:
    """Raw TCP connect check, used when HTTP is unreachable."""
    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


# ── NetworkManager signal ────────────────────────────────────────────────


async def nmcli_state(timeout: float = 2.0) -> str | None:
    """Read NetworkManager's state via ``nmcli -t -f STATE general``; None if unavailable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "STATE", "general",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return stdout.decode().strip() or None
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001 - degrade gracefully
        logger.debug("nmcli probe failed: %s", e)
        return None
    return None


# nmcli levels that mean "not connected" (from nmcli general state vocabulary)
_NM_BAD_LEAVE = {"disconnected", "asleep", "connecting", "connecting (deactivating)", "deactivating"}
_NM_PORTAL = {"portal", "limited"}


def _nm_verdict(state: str | None) -> str:
    """Collapse an nmcli state into ok / portal / bad / unknown."""
    if state is None:
        return "unknown"
    s = state.strip().lower()
    if s in _NM_BAD_LEAVE:
        return "bad"
    if s in _NM_PORTAL:
        return "portal"
    return "ok"


# ── NetWatch ─────────────────────────────────────────────────────────────


@dataclass
class _Probes:
    """Injectable probe functions, for tests."""

    internet: Callable[[NetWatchConfig], Awaitable[InternetProbeResult]]
    provider: Callable[[str, float], Awaitable[ProbeHTTPStatus]]
    nm_state: Callable[[], Awaitable[str | None]]


async def default_internet_probe(config: NetWatchConfig) -> InternetProbeResult:
    """Generic HTTP probe, falling back to a raw TCP connect.

    Captive-portal detection: the 204 endpoint must answer bare (204/empty).
    A redirect away from it, or a 200 with body content, or a portal keyword
    in the body all mean we are behind a captive portal.
    """
    start = time.monotonic()
    try:
        status, body, final_url = await http_probe(config.http_probe_url, config.http_timeout)
        latency = (time.monotonic() - start) * 1000
        redirected = final_url.rstrip("/") != config.http_probe_url.rstrip("/")
        # A 204 endpoint answering 200 with content, or bouncing elsewhere, is a portal.
        is_204_endpoint = "generate_204" in config.http_probe_url.lower()
        captive = (
            (redirected and is_204_endpoint)
            or (is_204_endpoint and status == 200 and body.strip())
            or any(k in body.lower() for k in ("captive portal", "wifi login", "sign in to"))
        )
        if captive:
            return InternetProbeResult(ok=False, latency_ms=latency, captive=True)
        return InternetProbeResult(ok=status in (200, 204), latency_ms=latency, captive=False)
    except Exception:
        pass
    # HTTP failed: try raw TCP
    try:
        await _tcp_probe(config.tcp_probe_host, config.tcp_probe_port, config.tcp_timeout)
        return InternetProbeResult(ok=True, latency_ms=(time.monotonic() - start) * 1000)
    except Exception:
        return InternetProbeResult(ok=False)


def _default_provider_probe(base_url: str, timeout: float) -> Awaitable[ProbeHTTPStatus]:
    return http_probe(base_url.rstrip("/") + "/v1/models", timeout)


async def default_nm_probe() -> str | None:
    return await nmcli_state()


class NetWatch:
    """Async connectivity monitor with an injectable probe set.

    Usage::

        nw = NetWatch()
        nw.add_provider("omniroute", "https://api.example.com/v1")
        await nw.start()
        state = await nw.wait_until_usable("omniroute")
    """

    def __init__(
        self,
        config: NetWatchConfig | None = None,
        *,
        internet_probe: Callable[[NetWatchConfig], Awaitable[InternetProbeResult]] | None = None,
        provider_probe: Callable[[str, float], Awaitable[ProbeHTTPStatus]] | None = None,
        nm_probe: Callable[[], Awaitable[str | None]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config or NetWatchConfig()
        self._sleep = sleep or asyncio.sleep
        self._probes = _Probes(
            internet=internet_probe or default_internet_probe,
            provider=provider_probe or _default_provider_probe,
            nm_state=nm_probe or default_nm_probe,
        )
        self._state = NetState.ONLINE
        self._providers: dict[str, ProviderProbeState] = {}
        self._callbacks: list[Callable[[NetState, NetState], Any]] = []
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._interval = self.config.base_interval
        self._last_nm_verdict: str = "unknown"
        self._wakeup: asyncio.Event | None = None

    # ── public API ──

    @property
    def state(self) -> NetState:
        return self._state

    def add_provider(self, name: str, base_url: str) -> None:
        self._providers[name] = ProviderProbeState(name=name, base_url=base_url.rstrip("/"))
        self._kick()  # re-probe promptly with the new provider

    def remove_provider(self, name: str) -> None:
        self._providers.pop(name, None)

    def provider_state(self, name: str) -> NetState | None:
        p = self._providers.get(name)
        return p.state if p else None

    def all_providers_down(self) -> bool:
        """True when every registered provider's endpoint is left as PROVIDER_DOWN."""
        probes = self._providers.values()
        if not probes:
            return False
        return all(p.state == NetState.PROVIDER_DOWN for p in probes)

    def subscribe(self, callback: Callable[[NetState, NetState], Any]) -> None:
        """Subscribe to (old_state, new_state) changes."""
        self._callbacks.append(callback)

    def next_interval(self) -> float:
        return self._interval

    async def wait_until_usable(self, provider: str | None = None) -> NetState:
        """Wait until the network is usable for the given provider (or generally).

        - provider=None: usable means not OFFLINE/CAPTIVE.
        - provider named: usable when that provider's endpoint is up, or when
          global connectivity is fine (the *router* handles provider failover).
        PROVIDER_DOWN for one provider never means OFFLINE — it means "fail over".
        """
        if self._wakeup is not None:
            self._wakeup.set()  # kick a fresh probe on first waiter
        while True:
            if provider is not None:
                prov = self._providers.get(provider)
                if prov is not None and prov.state in (NetState.ONLINE, NetState.DEGRADED):
                    return self._state
            if self._state.usable_for_llm:
                return self._state
            await self._sleep(self._interval)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._wakeup is None:
            self._wakeup = asyncio.Event()
        # Immediate initial probe (synchronous, but fast).
        self._tasks.append(asyncio.create_task(self._monitor_loop()))
        self._tasks.append(asyncio.create_task(self._nm_watch_loop()))

    async def stop(self) -> None:
        self._running = False
        if self._wakeup:
            self._wakeup.set()
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

    async def re_probe(self) -> None:
        """Force a full probe cycle now (also triggered by NM state changes)."""
        if self._wakeup:
            self._wakeup.set()

    # ── internals ──

    def _kick(self) -> None:
        if self._wakeup:
            self._wakeup.set()

    async def _monitor_loop(self) -> None:
        assert self._wakeup is not None
        while self._running:
            try:
                # Wake immediately when kicked (NM change, new provider, re_probe);
                # otherwise sleep out the current adaptive interval.
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=self._interval)
                    self._wakeup.clear()
                except TimeoutError:
                    pass
                await self._probe_all()
                # 5 s while healthy, backoff (×2) toward 60 s while bad.
                self._interval = (
                    self.config.base_interval
                    if self._state.usable_for_llm
                    else min(self._interval * self.config.backoff_factor, self.config.max_interval)
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - monitor must never crash
                logger.debug("NetWatch monitor error: %s", e)

    async def _nm_watch_loop(self) -> None:
        """Poll nmcli for NetworkManager state changes; re-probe immediately on change."""
        assert self._wakeup is not None
        while self._running:
            try:
                state = await self._probes.nm_state()
                verdict = _nm_verdict(state)
                if verdict != self._last_nm_verdict:
                    changed_from_ok = self._last_nm_verdict == "ok"
                    self._last_nm_verdict = verdict
                    if verdict in ("bad", "portal") or (changed_from_ok and verdict != "unknown"):
                        self._kick()  # immediate re-probe
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("NetWatch NM watch error: %s", e)
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.config.nm_poll_interval)
                self._wakeup.clear()
            except TimeoutError:
                pass

    async def _probe_all(self) -> None:
        internet = await self._probes.internet(self.config)
        provider_states = await self._probe_providers()
        new_state = self._compute_state(internet, provider_states)
        if new_state is not self._state:
            old, self._state = self._state, new_state
            logger.info("NetWatch state: %s -> %s", old.value, new_state.value)
            for cb in list(self._callbacks):
                try:
                    res = cb(old, new_state)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:  # noqa: BLE001 - subscriber errors must not matter
                    logger.debug("NetWatch callback error: %s", e)

    async def _probe_providers(self) -> dict[str, NetState]:
        results: dict[str, NetState] = {}
        for name, prov in self._providers.items():
            try:
                status, _body, _final = await self._probes.provider(
                    prov.base_url, self.config.provider_probe_timeout
                )
                if 0 < status < 500:
                    prov.state = NetState.ONLINE
                    prov.last_ok = time.monotonic()
                    prov.consecutive_fails = 0
                else:
                    prov.consecutive_fails += 1
                    prov.last_fail = time.monotonic()
                    if prov.consecutive_fails >= self.config.provider_fail_threshold:
                        prov.state = NetState.PROVIDER_DOWN
            except Exception:
                prov.consecutive_fails += 1
                prov.last_fail = time.monotonic()
                if prov.consecutive_fails >= self.config.provider_fail_threshold:
                    prov.state = NetState.PROVIDER_DOWN
            results[name] = prov.state
        return results

    def _compute_state(self, internet: InternetProbeResult, provider_states: dict[str, NetState]) -> NetState:
        if internet.captive:
            return NetState.CAPTIVE
        if not internet.ok:
            return NetState.OFFLINE
        # Internet works: a specific provider being down is not offline.
        if provider_states and self.all_providers_down():
            return NetState.PROVIDER_DOWN
        if internet.latency_ms is not None and internet.latency_ms > self.config.degraded_threshold_ms:
            return NetState.DEGRADED
        return NetState.ONLINE
