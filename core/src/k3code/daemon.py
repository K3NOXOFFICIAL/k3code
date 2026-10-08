"""``k3code daemon``: a long-lived gateway host that keeps sessions running while no TUI is attached.

Listens on ``$K3CODE_HOME/run/gateway.sock`` (JSON-RPC, one connection per client). Under systemd it
sends READY=1 and a WATCHDOG=1 ping every 30 s. A restart-storm guard starts the daemon with background
work paused when it was restarted more than 5 times in 10 minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from k3code import sdnotify

logger = logging.getLogger("k3code.daemon")

RESTART_WINDOW_S = 600.0
RESTART_LIMIT = 5
WATCHDOG_INTERVAL_S = 30.0
#: Safe mode lifts itself after this long with no daemon start, at most SAFE_MODE_MAX_AUTO_CLEARS times per day.
SAFE_MODE_QUIET_S = 1800.0
SAFE_MODE_MAX_AUTO_CLEARS = 3
SAFE_MODE_CLEAR_WINDOW_S = 86400.0
HOUSEKEEPING_EVERY_S = 60.0


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


def _stamps(path: Path) -> list[float]:
    with contextlib.suppress(OSError, ValueError, TypeError):
        return [float(t) for t in json.loads(path.read_text())]
    return []


def auto_clears_file(home: Path | None = None) -> Path:
    return (home or k3_home()) / "run" / "safe_mode_clears.json"


def maybe_auto_clear(server: Any, home: Path | None = None, now: float | None = None) -> bool:
    """Leave restart-storm safe mode once the daemon has been quiet for SAFE_MODE_QUIET_S.

    Limited to SAFE_MODE_MAX_AUTO_CLEARS per SAFE_MODE_CLEAR_WINDOW_S: after that the daemon stays paused until a
    human runs ``/daemon resume``. A halt (``/daemon pause``) overrides this. Returns whether it cleared safe mode.
    """
    home = home or k3_home()
    now = time.time() if now is None else now
    if not server.background_paused or server.halted:
        return False
    starts = _stamps(restarts_file(home))
    if now - max(starts, default=0.0) < SAFE_MODE_QUIET_S:
        return False
    clears_path = auto_clears_file(home)
    clears = [t for t in _stamps(clears_path) if now - t < SAFE_MODE_CLEAR_WINDOW_S]
    if len(clears) >= SAFE_MODE_MAX_AUTO_CLEARS:
        server.safe_mode_notice = (
            f"{SAFE_MODE_NOTICE} The automatic clear limit ({SAFE_MODE_MAX_AUTO_CLEARS} per 24 h) is reached: "
            "background work stays paused until /daemon resume."
        )
        return False
    clears.append(now)
    clears_path.parent.mkdir(parents=True, exist_ok=True)
    clears_path.write_text(json.dumps(clears))
    server.background_paused = False
    server.safe_mode_notice = ""
    server.emit("notification.clear", {"key": "k3.safe_mode"}, importance="essential")
    server.emit(
        "notification.show",
        {
            "text": f"Safe mode cleared automatically after {int(SAFE_MODE_QUIET_S // 60)} min without a restart.",
            "level": "info",
            "kind": "daemon",
            "key": "k3.safe_mode.cleared",
        },
        importance="essential",
    )
    logger.warning("restart-storm safe mode cleared automatically (%d of %d in 24 h)", len(clears),
                   SAFE_MODE_MAX_AUTO_CLEARS)
    return True


async def _housekeeping(server: Any, home: Path) -> None:
    """Once a minute: the safe-mode auto-clear, then the goal watchdog (which re-kicks goals with no live turn)."""
    while True:
        await asyncio.sleep(HOUSEKEEPING_EVERY_S)
        with contextlib.suppress(Exception):
            maybe_auto_clear(server, home)
        with contextlib.suppress(Exception):
            await server.watchdog_tick()


SAFE_MODE_NOTICE = (
    "k3code daemon restarted more than 5 times in 10 minutes: background work is paused. "
    "Fix the cause, then run /daemon resume."
)


class DaemonAlreadyRunning(RuntimeError):
    """Another k3code daemon holds this home's instance lock."""


def acquire_instance_lock(sock: Path) -> int:
    """Take an exclusive, non-blocking flock on ``<run dir>/daemon.lock``; returns the fd to keep open.

    Without it a second ``k3code daemon`` (a manual start next to the systemd unit, the 5 s RestartSec window) unlinked
    the live socket and rebound it: the first daemon kept running its sessions, cron and loops but became unreachable,
    and stopping either one then deleted the other's socket.
    """
    sock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(sock.parent / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise DaemonAlreadyRunning(f"another k3code daemon is already running ({sock})") from None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


async def run_daemon(
    *,
    home: Path | None = None,
    sock: Path | None = None,
    watchdog_interval: float | None = None,
    install_signals: bool = True,
    ready_event: asyncio.Event | None = None,
    server_out: list | None = None,
) -> None:
    """Run until SIGTERM/SIGINT (or ``server.request_stop()``)."""
    from k3code.gateway.server import GatewayServer

    home = home or k3_home()
    sock = sock or socket_path(home)
    lock_fd = acquire_instance_lock(sock)  # refuses to start a second daemon on this home (held until we exit)
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
    if serve.done():
        serve.result()  # the server failed to come up: raise its error instead of announcing READY=1
    await server.ensure_automation()  # resume loops, start the cron scheduler and triggers
    await server.resume_goals()  # goals that were active (or paused by a graceful stop) continue
    sdnotify.ready()
    logger.info("daemon ready on %s", sock)
    if ready_event is not None:
        ready_event.set()
    interval = sdnotify.watchdog_interval(WATCHDOG_INTERVAL_S) if watchdog_interval is None else watchdog_interval
    dog = asyncio.create_task(sdnotify.watchdog_loop(interval))
    housekeeping = asyncio.create_task(_housekeeping(server, home))
    try:
        await serve
    finally:
        sdnotify.stopping()
        dog.cancel()
        housekeeping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dog
        with contextlib.suppress(asyncio.CancelledError):
            await housekeeping
        await server.close()
        with contextlib.suppress(OSError):
            os.close(lock_fd)


async def attach_bridge(sock: Path | None = None, *, readonly: bool = False) -> int:
    """Pump this process's stdin/stdout to the daemon socket (``k3code gateway --attach``).

    Inside a k3 pane (``$TUIOS_SOCKET``) the bridge also mirrors the session's state into tuios, holds approvals in
    the tuios Inbox and opens new panes for ``open_pane`` results. ``readonly`` refuses everything that would change
    the session (``k3code attach --readonly``).
    """
    import sys

    from k3code.integrations.panes import PaneLink

    sock = sock or socket_path()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock), limit=1 << 26)
    except OSError as e:
        sys.stderr.write(f"k3code: cannot attach to the daemon at {sock}: {e}\nStart it with `k3code daemon`.\n")
        return 1
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=1 << 26)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)

    def emit(line: str) -> None:
        sys.stdout.buffer.write(line.encode() + b"\n")
        sys.stdout.buffer.flush()

    def inject(req_id: str, method: str, result: dict) -> None:  # the Inbox answered (called from a thread)
        def go() -> None:
            writer.write((json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n").encode())
            emit(json.dumps({"jsonrpc": "2.0", "id": f"cancel-{req_id}", "method": "request.cancel",
                             "params": {"id": req_id, "method": method, "reason": "answered in the Inbox"}}))

        loop.call_soon_threadsafe(go)

    link = PaneLink.from_env(inject, readonly=readonly)
    if link is not None:
        link.reporter.report("idle")

    async def up() -> None:
        if link is None:
            while chunk := await stdin.read(65536):
                writer.write(chunk)
                await writer.drain()
        else:
            while raw := await stdin.readline():
                text = raw.decode("utf-8", errors="replace").strip()
                verdict = link.on_client_line(text) if text else None
                if verdict is not None:  # read-only pane: answer it ourselves, never forward
                    if verdict:
                        emit(verdict)
                    continue
                writer.write(raw if raw.endswith(b"\n") else raw + b"\n")
                await writer.drain()
        writer.write_eof()

    async def down() -> None:
        if link is None:
            while chunk := await reader.read(65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            return
        while raw := await reader.readline():
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
            link.on_server_line(raw.decode("utf-8", errors="replace").strip())

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    if link is not None:
        link.reporter.report_tuios("none")
        link.reporter.flush(2.0)
    with contextlib.suppress(Exception):
        writer.close()
    return 0


async def tail_subagent(subagent_id: str, sock: Path | None = None, *, interval: float = 1.0, out: Any = None) -> int:
    """Follow a sub-agent of the daemon read-only (``k3code tail``): print its tail until it finishes."""
    import sys

    out = out or sys.stdout
    sock = sock or socket_path()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock), limit=1 << 26)
    except OSError as e:
        out.write(f"k3code: cannot attach to the daemon at {sock}: {e}\n")
        return 1
    shown = ""
    seq = 0
    try:
        while True:
            seq += 1
            req = {"jsonrpc": "2.0", "id": seq, "method": "subagent.tail", "params": {"subagent_id": subagent_id}}
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            resp: dict[str, Any] = {}
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == seq and "method" not in msg:
                    resp = msg.get("result") or {}
                    break
            else:
                return 1
            text = str(resp.get("text") or "")
            if text.startswith(shown):
                out.write(text[len(shown):])
            elif text != shown:  # the 30-line window moved on: show what is new at the end
                out.write("\n" + text)
            out.flush()
            shown = text
            if resp.get("done") or resp.get("status") == "unknown":
                out.write(f"\n[{resp.get('status', 'done')}]\n")
                return 0
            await asyncio.sleep(interval)
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def slash_via_daemon(command: str, *, cwd: str, sock: Path | None = None, session_id: str | None = None) -> str:
    """Run one slash command (``/automations list`` …) against the running daemon and return its output text.

    Uses a throwaway session when ``session_id`` is not given. Answers any ``clarify`` request with its first choice.
    """
    sock = sock or socket_path()
    reader, writer = await asyncio.open_unix_connection(str(sock))
    pending: dict[int, asyncio.Future[dict]] = {}
    seq = 0

    async def pump() -> None:
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") in pending and "method" not in msg:
                pending[msg["id"]].set_result(msg)
            elif msg.get("method") == "clarify" and "id" in msg:
                choices = (msg.get("params") or {}).get("choices") or [""]
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"answer": choices[0]}}
                writer.write((json.dumps(reply) + "\n").encode())

    async def call(method: str, params: dict) -> dict:
        nonlocal seq
        seq += 1
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        pending[seq] = fut
        writer.write((json.dumps({"jsonrpc": "2.0", "id": seq, "method": method, "params": params}) + "\n").encode())
        await writer.drain()
        return await asyncio.wait_for(fut, 60)

    task = asyncio.create_task(pump())
    throwaway = session_id is None
    try:
        if session_id is None:
            created = await call("session.create", {"cwd": cwd})
            session_id = created["result"]["session_id"]
        res = await call("slash.exec", {"command": command.lstrip("/"), "session_id": session_id})
        if "error" in res:
            return f"error: {res['error'].get('message', res['error'])}"
        return str(res["result"].get("output") or res["result"].get("message") or "")
    finally:
        if throwaway and session_id is not None:
            with contextlib.suppress(Exception):
                await call("session.delete", {"session_id": session_id})
        task.cancel()
        writer.close()
