"""The gateway over a real Unix socket: clarify round trips, a stalled peer, a graceful stop with a client attached."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

from k3code.bundle import write_bundle
from m1cmd_helpers import make_server


class Peer:
    """A minimal JSON-RPC client on the daemon's socket."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer, self._id = reader, writer, 0

    @classmethod
    async def connect(cls, path: Path) -> Peer:
        reader, writer = await asyncio.open_unix_connection(str(path))
        return cls(reader, writer)

    async def frame(self, timeout: float = 10.0) -> dict:
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        assert line, "connection closed"
        return json.loads(line)

    async def send(self, method: str, params: dict) -> int:
        self._id += 1
        frame = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        self.writer.write((json.dumps(frame) + "\n").encode())
        await self.writer.drain()
        return self._id

    async def until(self, pred, timeout: float = 10.0) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                f = await self.frame(timeout)
                if pred(f):
                    return f


@pytest.fixture
async def daemon(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sock = tmp_path / "k3.sock"
    await server.start_socket(sock)
    server._running = True
    yield server, sock
    await server.stop_socket()
    await server.close()


async def test_a_command_that_asks_the_client_does_not_deadlock_its_own_connection(daemon, tmp_path):
    """Handlers were awaited inside the read loop, so a command waiting for the client's answer (clarify) blocked
    the very loop that had to read that answer: /import, /schedule, /automations, update-config all hung."""
    server, sock = daemon
    sid = (await _create_session(server, sock, tmp_path))[1]
    bundle = tmp_path / "x.k3bundle"
    write_bundle(bundle, store=server.store, cwd=tmp_path, session_ids=[sid], settings=False)
    peer = await Peer.connect(sock)
    await peer.until(lambda f: f.get("method") == "event" or "gateway.ready" in json.dumps(f))
    created = await peer.send("session.create", {"cwd": str(tmp_path)})
    await peer.until(lambda f: f.get("id") == created)
    req = await peer.send("slash.exec", {"command": f"import {bundle}", "session_id": None})
    ask = await peer.until(lambda f: f.get("method") == "clarify")  # the server asks us; our read loop must be free
    answer = {"jsonrpc": "2.0", "id": ask["id"], "result": {"answer": "Cancel"}}
    peer.writer.write((json.dumps(answer) + "\n").encode())
    await peer.writer.drain()
    done = await peer.until(lambda f: f.get("id") == req)
    assert "cancelled" in json.dumps(done).lower()


async def _create_session(server, sock, tmp_path):
    from m1cmd_helpers import new_session

    return None, await new_session(server, tmp_path)


async def test_a_peer_that_stops_reading_is_dropped_instead_of_bloating_the_daemon(daemon):
    server, sock = daemon
    server.MAX_CLIENT_BACKLOG = 256 * 1024
    stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stalled.connect(str(sock))  # connects, never reads
    healthy = await Peer.connect(sock)
    received = 0

    async def read_all() -> None:
        nonlocal received
        while line := await healthy.reader.readline():
            received += b'"noise"' in line

    reader_task = asyncio.create_task(read_all())
    for _ in range(100):
        if len([c for c in server.clients if c.name.startswith("socket#")]) == 2:
            break
        await asyncio.sleep(0.02)
    for i in range(400):
        server.broadcast("noise", {"i": i, "pad": "x" * 4000})  # ~1.6 MB toward both peers
        if i % 20 == 0:
            await asyncio.sleep(0.005)  # let the healthy peer's reader run
    await asyncio.sleep(0.5)
    attached = [c.name for c in server.clients if c.name.startswith("socket#")]
    assert attached == ["socket#2"], f"only the stalled client (socket#1) should be gone, got {attached}"
    assert received > 300  # the healthy client kept receiving through it all
    reader_task.cancel()
    stalled.close()


async def test_graceful_stop_finishes_while_a_client_is_attached(tmp_path, monkeypatch):
    """wait_closed() waits for every connection (CPython >= 3.12): the stop hung until systemd's SIGKILL."""
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sock = tmp_path / "stop.sock"
    await server.start_socket(sock)
    peer = await Peer.connect(sock)
    await peer.frame()
    await asyncio.wait_for(server.stop_socket(), 10)
    assert (await asyncio.wait_for(peer.reader.read(), 5)) is not None  # EOF reached: the daemon hung up on us
    await server.close()
