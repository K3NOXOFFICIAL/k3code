"""SSRF guard for the web tools: refuse URLs whose host resolves to a loopback, private, link-local, multicast,
reserved, unspecified, site-local or carrier-grade-NAT address (and IPv4-mapped IPv6 forms of those).

The host is taken exactly as httpx sends it (``httpx.URL(url).raw_host``: IDNA2008, lower case), so the guard never
vets a different name than the one the client connects to. IP literals, an IPv6 zone id included, are checked by
``ipaddress`` without a lookup; any other host goes through ``socket.getaddrinfo``, which also normalises the
decimal/hex IPv4 forms (``http://2130706433/``, ``http://0x7f.1/``). The guard fails closed: a URL httpx cannot parse
or encode, and a name that does not resolve, are refused. ``check_url`` is blocking: async callers use ``acheck_url``.

DNS rebinding: ``WebFetcher`` connects through ``GuardedTransport``, whose network backend resolves the name once at
connect time, vets every answer and connects to a vetted address (TLS SNI and the Host header keep the name), so the
address that was checked is the address that is reached. Not pinned, stated plainly:
* the browser: Chromium resolves names itself. ``BrowserManager`` vets every request, popup and WebSocket through
  Playwright routes, but a name that answers differently to Chromium than to this process is not caught.
* an HTTP(S) proxy from the environment: httpx sends those requests through the proxy, which does its own lookup.
Opt out with ``web.allow_private: true``; the exact origin (scheme, host, port) of ``research.searxng_url`` always
passes, at the connection layer as its (host, port), the only parts a TCP connect sees.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

import httpcore
import httpx

Origin = tuple[str, str, int]  # (scheme, host as raw_host, port with the scheme's default filled in)

DEFAULT_PORTS = {"http": 80, "https": 443}


class BlockedURL(RuntimeError):
    """The URL is not http(s), or its host resolves to an address the guard refuses."""


def _parse(url: str) -> Origin:
    """(scheme, host, port) as httpx will connect to them. Raises ``BlockedURL`` for what httpx cannot parse."""
    try:
        u = httpx.URL(url)
        host = u.raw_host.decode("ascii").lower()
    except (httpx.InvalidURL, UnicodeError, ValueError, TypeError) as e:
        raise BlockedURL(f"blocked: not a valid URL: {url[:200]}") from e
    if u.scheme not in DEFAULT_PORTS or not host:
        raise BlockedURL(f"blocked: not an http(s) URL: {url[:200]}")
    return u.scheme, host, u.port or DEFAULT_PORTS[u.scheme]


def origin_of(url: str) -> Origin | None:
    try:
        return _parse(url)
    except BlockedURL:
        return None


def web_settings(config: Any) -> tuple[bool, frozenset[Origin]]:
    """(allow_private, allow_origins) from a config object: ``web.allow_private`` plus the origin of
    ``research.searxng_url``, which must keep working when it is a local instance."""
    web = dict(getattr(config, "web", None) or {})
    research = dict(getattr(config, "research", None) or {})
    origin = origin_of(str(research.get("searxng_url") or ""))
    return bool(web.get("allow_private", False)), frozenset({origin} if origin else ())


def _refused(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
        or not ip.is_global  # carrier-grade NAT (100.64/10) and whatever else is not publicly routable
    )


def _resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to ([] when it does not). A seam: the tests replace it, never the network."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])  # drop an IPv6 zone id (``%25eth0`` in a URL)
    except ValueError:
        return None


def vetted_addresses(host: str) -> list[str]:
    """The addresses ``host`` (a raw_host) reaches, each of them public. Raises ``BlockedURL`` when any answer is
    private/loopback, when an answer is not an address, or when the name does not resolve."""
    if (ip := _literal(host)) is not None:
        if _refused(ip):
            raise BlockedURL(
                f"blocked: {host} resolves to a private/loopback address (set web.allow_private: true to allow)"
            )
        return [str(ip)]
    answers = _resolve(host)
    if not answers:
        raise BlockedURL(f"blocked: {host} could not be resolved")
    for raw in answers:
        ip = _literal(raw)
        if ip is None or _refused(ip):
            raise BlockedURL(
                f"blocked: {host} resolves to a private/loopback address (set web.allow_private: true to allow)"
            )
    return answers


def check_url(url: str, *, allow_private: bool = False, allow_origins: Iterable[Origin] = ()) -> None:
    """Raises ``BlockedURL`` unless ``url`` is http(s) and every address its host resolves to is public."""
    origin = _parse(url)
    if allow_private or origin in set(allow_origins):
        return
    vetted_addresses(origin[1])


async def acheck_url(url: str, *, allow_private: bool = False, allow_origins: Iterable[Origin] = ()) -> None:
    await asyncio.to_thread(check_url, url, allow_private=allow_private, allow_origins=tuple(allow_origins))


# ── pinning: the connection goes to the address that was vetted ──


class PinningBackend(httpcore.AsyncNetworkBackend):
    """Resolves and vets the host at connect time, then connects to a vetted address, so a name that answers
    differently on a second lookup (DNS rebinding) cannot reach a private address. (host, port) pairs in
    ``allow_hosts`` connect as asked."""

    def __init__(self, allow_hosts: Iterable[tuple[str, int]] = (), inner: httpcore.AsyncNetworkBackend | None = None):
        self._allow = frozenset(allow_hosts)
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (host.lower(), port) in self._allow:
            addresses = [host]
        else:
            addresses = await asyncio.to_thread(vetted_addresses, host.lower())
        kw = {"timeout": timeout, "local_address": local_address, "socket_options": socket_options}
        for address in addresses[:-1]:  # every answer is vetted: try them in resolver order, as a plain connect would
            with suppress(httpcore.ConnectError, httpcore.ConnectTimeout, OSError):
                return await self._inner.connect_tcp(address, port, **kw)
        return await self._inner.connect_tcp(addresses[-1], port, **kw)

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport with a ``PinningBackend``. httpx has no public hook for the network backend, so the pool is
    rebuilt with httpx's default limits and TLS context; ``BlockedURL`` propagates out of the request unchanged."""

    def __init__(self, allow_origins: Iterable[Origin] = (), inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        super().__init__()
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)  # httpx.DEFAULT_LIMITS
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=PinningBackend(((host, port) for _s, host, port in allow_origins), inner),
        )
