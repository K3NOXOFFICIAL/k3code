"""Minimal systemd notify client (``sd_notify``) over the stdlib: READY=1, WATCHDOG=1, STOPPING=1."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket


def notify(message: str, env: dict[str, str] | None = None) -> bool:
    """Send ``message`` to ``$NOTIFY_SOCKET``; False when not under systemd (or the send failed)."""
    addr = (env if env is not None else os.environ).get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(addr)
            sock.sendall(message.encode("utf-8"))
        return True
    except OSError:
        return False


def ready() -> bool:
    return notify("READY=1")


def stopping() -> bool:
    return notify("STOPPING=1")


def watchdog_interval(default: float = 30.0) -> float:
    """Ping at most every ``default`` s, and at half of systemd's ``WATCHDOG_USEC`` if that is shorter."""
    usec = os.environ.get("WATCHDOG_USEC")
    if usec and usec.isdigit() and int(usec) > 0:
        return min(default, int(usec) / 2_000_000)
    return default


async def watchdog_loop(interval: float = 30.0) -> None:
    """Send ``WATCHDOG=1`` every ``interval`` seconds until cancelled."""
    with contextlib.suppress(asyncio.CancelledError):
        while True:
            notify("WATCHDOG=1")
            await asyncio.sleep(interval)
