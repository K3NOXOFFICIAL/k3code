"""``k3code daemon``: a long-lived gateway host that keeps sessions running while no TUI is attached.

Listens on ``$K3CODE_HOME/run/gateway.sock`` (JSON-RPC, one connection per client). Under systemd it
sends READY=1 and a WATCHDOG=1 ping every 30 s. A restart-storm guard starts the daemon with background
work paused when it was restarted more than 5 times in 10 minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from pathlib import Path

from k3code import sdnotify

logger = logging.getLogger("k3code.daemon")

RESTART_WINDOW_S = 600.0
RESTART_LIMIT = 5
WATCHDOG_INTERVAL_S = 30.0


def k3_home() -> Path:
    return Path(os.environ.get("K3CODE_HOME", str(Path.home() / ".k3code"))).expanduser()


def socket_path(home: Path | None = None) -> Path:
    """Where clients attach: ``K3CODE_GATEWAY_SOCKET`` / ``HERMES_TUI_GATEWAY_URL`` or the home default."""
    for var in ("K3CODE_GATEWAY_SOCKET", "HERMES_TUI_GATEWAY_URL"):
        raw = os.environ.get(var)
        if raw:
            return Path(raw.removeprefix("unix://")).expanduser()
    return (home or k3_home()) / "run" / "gateway.sock"


def restarts_file(home: Path | None = None) -> Path:
    return (home or k3_home()) / "run" / "restarts.json"


def record_restart(home: Path | None = None, now: float | None = None) -> int:
    """Note a daemon start; returns how many starts (including this one) happened in the last 10 minutes."""
    path = restarts_file(home)
    now = time.time() if now is None else now
    stamps: list[float] = []
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(path.read_text())
        stamps = [float(t) for t in data if now - float(t) < RESTART_WINDOW_S]
    stamps.append(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamps))
    return len(stamps)


def storm_active(count: int) -> bool:
    return count > RESTART_LIMIT


SAFE_MODE_NOTICE = (
    "k3code daemon restarted more than 5 times in 10 minutes: background work is paused. "
    "Fix the cause, then run /daemon resume."
)


async def run_daemon(
    *,
    home: Path | None = None,
    sock: Path | None = None,
    watchdog_interval: float = WATCHDOG_INTERVAL_S,
    install_signals: bool = True,
    ready_event: asyncio.Event | None = None,
    server_out: list | None = None,
) -> None:
    """Run until SIGTERM/SIGINT (or ``server.request_stop()``)."""
    from k3code.gateway.server import GatewayServer

    home = home or k3_home()
    sock = sock or socket_path(home)
    count = record_restart(home)
    server = GatewayServer()
    if server_out is not None:
        server_out.append(server)
    if storm_active(count):
        server.background_paused = True
        server.safe_mode_notice = SAFE_MODE_NOTICE
        logger.warning("restart storm (%d starts in 10 min): safe mode, background work paused", count)
    if install_signals:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, server.request_stop)
    serve = asyncio.create_task(server.serve(stdio=False, socket_path=sock))
    # Wait for the socket before telling systemd we are ready.
    for _ in range(200):
        if sock.exists() or serve.done():
            break
        await asyncio.sleep(0.05)
    sdnotify.ready()
    logger.info("daemon ready on %s", sock)
    if ready_event is not None:
        ready_event.set()
    dog = asyncio.create_task(sdnotify.watchdog_loop(watchdog_interval))
    try:
        await serve
    finally:
        sdnotify.stopping()
        dog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dog
        await server.close()


async def attach_bridge(sock: Path | None = None) -> int:
    """Pump this process's stdin/stdout to the daemon socket (``k3code gateway --attach``)."""
    import sys

    sock = sock or socket_path()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
    except OSError as e:
        sys.stderr.write(f"k3code: cannot attach to the daemon at {sock}: {e}\nStart it with `k3code daemon`.\n")
        return 1
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)

    async def up() -> None:
        while chunk := await stdin.read(65536):
            writer.write(chunk)
            await writer.drain()
        writer.write_eof()

    async def down() -> None:
        while chunk := await reader.read(65536):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    with contextlib.suppress(Exception):
        writer.close()
    return 0
