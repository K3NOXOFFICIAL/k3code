"""SSRF guard for the web tools: refuse URLs whose host resolves to a loopback, private, link-local, multicast,
reserved, unspecified, site-local or carrier-grade-NAT address (and IPv4-mapped IPv6 forms of those).

``check_url`` resolves through ``socket.getaddrinfo``, which also normalises decimal/octal/hex IPv4 literals
(``http://2130706433/``, ``http://0x7f.1/``). It is blocking: async callers use ``acheck_url``.

Known limits, stated plainly:
* DNS rebinding: the vetted answer is not pinned, so the HTTP client resolves the name again when it connects.
* Browser redirects and sub-requests happen inside Chromium; ``BrowserManager`` vets each request through a Playwright
  route handler, but a name Chromium resolves differently from this process is not caught either.
Opt out with ``web.allow_private: true``; hosts listed in ``allow_hosts`` (the configured SearXNG) always pass.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse


class BlockedURL(RuntimeError):
    """The URL is not http(s), or its host resolves to an address the guard refuses."""


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def web_settings(config: Any) -> tuple[bool, frozenset[str]]:
    """(allow_private, allow_hosts) from a config object: ``web.allow_private`` plus the host of
    ``research.searxng_url``, which must keep working when it is a local instance."""
    web = dict(getattr(config, "web", None) or {})
    research = dict(getattr(config, "research", None) or {})
    host = host_of(str(research.get("searxng_url") or ""))
    return bool(web.get("allow_private", False)), frozenset({host} if host else ())


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
        return []  # unresolvable: the request itself fails with its own error, there is nothing to reach
    return [str(info[4][0]) for info in infos]


def check_url(url: str, *, allow_private: bool = False, allow_hosts: Iterable[str] = ()) -> None:
    """Raises ``BlockedURL`` unless ``url`` is http(s) and every address its host resolves to is public."""
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        _ = u.port  # raises ValueError on a malformed port
    except ValueError as e:
        raise BlockedURL(f"blocked: not a valid URL: {url[:200]}") from e
    if u.scheme not in ("http", "https") or not host:
        raise BlockedURL(f"blocked: not an http(s) URL: {url[:200]}")
    if allow_private or host in {h.lower() for h in allow_hosts}:
        return
    for raw in _resolve(host):
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])  # drop an IPv6 zone id
        except ValueError:
            continue
        if _refused(ip):
            raise BlockedURL(
                f"blocked: {host} resolves to a private/loopback address (set web.allow_private: true to allow)"
            )


async def acheck_url(url: str, *, allow_private: bool = False, allow_hosts: Iterable[str] = ()) -> None:
    await asyncio.to_thread(check_url, url, allow_private=allow_private, allow_hosts=tuple(allow_hosts))
