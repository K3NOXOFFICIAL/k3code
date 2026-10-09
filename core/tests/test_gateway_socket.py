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


async def test_a_frame_over_64_kib_is_read_and_an_over_limit_one_is_answered_not_fatal(tmp_path, monkeypatch):
    """asyncio's default 64 KiB line limit made a pasted prompt raise out of the read loop: the client was cut."""
    import k3code.gateway.server as gateway_server

    monkeypatch.setattr(gateway_server, "MAX_FRAME_BYTES", 512 * 1024)
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sock = tmp_path / "big.sock"
    await server.start_socket(sock)
    server._running = True
    peer = await Peer.connect(sock)
    await peer.frame()
    big = await peer.send("no.such.method", {"pad": "x" * 200_000})
    assert (await peer.until(lambda f: f.get("id") == big))["error"]
    await peer.send("no.such.method", {"pad": "x" * 600_000})  # over the limit: an error frame, no hang-up
    assert "longer than" in (await peer.until(lambda f: "error" in f and f.get("id") is None))["error"]["message"]
    after = await peer.send("no.such.method", {})
    assert (await peer.until(lambda f: f.get("id") == after))["error"]
    await server.stop_socket()
    await server.close()


async def test_stdio_survives_an_over_limit_frame(tmp_path, monkeypatch):
    import k3code.gateway.server as gateway_server

    monkeypatch.setattr(gateway_server, "MAX_FRAME_BYTES", 1024)
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    stdin = asyncio.StreamReader(limit=1024)
    server._stdin = stdin
    server._running = True
    stdin.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"x","params":{"pad":"' + b"x" * 4096 + b'"}}\n')
    stdin.feed_data(b'{"jsonrpc":"2.0","id":2,"method":"no.such.method","params":{}}\n')
    stdin.feed_eof()
    await asyncio.wait_for(server._serve_stdio(), 10)
    frames = [json.loads(x) for x in server._frames]
    assert any("longer than" in json.dumps(f) for f in frames)
    assert any(f.get("id") == 2 for f in frames)  # the loop kept reading after the over-long frame
    await server.close()


async def _peer_on_new_session(sock: Path, tmp_path: Path) -> tuple[Peer, str]:
    peer = await Peer.connect(sock)
    created = await peer.send("session.create", {"cwd": str(tmp_path)})
    return peer, (await peer.until(lambda f: f.get("id") == created))["result"]["session_id"]


async def _until_detached(server, sockets_left: int) -> None:
    """The daemon has run its detach bookkeeping (client removed and the reap decided, in one synchronous step)."""
    async with asyncio.timeout(5):
        while len([c for c in server.clients if c.name.startswith("socket#")]) != sockets_left:
            await asyncio.sleep(0.01)


async def _until_not_live(server, sid: str) -> None:
    async with asyncio.timeout(5):
        while sid in server.live:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("hang_up", ["goodbye", "crash"])
async def test_an_empty_idle_session_is_closed_when_its_last_client_leaves(daemon, tmp_path, hang_up):
    """Every TUI start creates a session; session.close refuses while its caller is attached, so an empty one stayed
    live for the daemon's lifetime after the TUI exited, crashed or was killed."""
    server, sock = daemon
    peer, sid = await _peer_on_new_session(sock, tmp_path)
    assert sid in server.live
    if hang_up == "goodbye":
        peer.writer.close()
    else:  # kill -9: the socket drops without a goodbye
        peer.writer.transport.abort()
    await _until_detached(server, 0)
    await _until_not_live(server, sid)
    assert server.store.get(sid) is not None  # only dropped from the live registry


class _RunningSubagent:
    def __init__(self, parent_sid: str) -> None:
        self.parent_sid, self.done, self.status, self.id = parent_sid, False, "running", "sa-1"


@pytest.mark.parametrize("busy", ["message", "working", "background", "automation", "queued", "subagent"])
async def test_a_session_with_content_or_work_survives_its_last_client_leaving(daemon, tmp_path, busy):
    server, sock = daemon
    peer, sid = await _peer_on_new_session(sock, tmp_path)
    live = server.live[sid]
    if busy == "message":
        live.stored.messages.append({"role": "user", "content": "hi"})
    elif busy == "working":
        live.streaming = True
    elif busy == "background":
        live.background = True
    elif busy == "automation":
        live.stored.meta["origin"] = "automation"
    elif busy == "queued":
        live.pending_prompts.append("next")
    else:
        server.subagents.handles["sa-1"] = _RunningSubagent(sid)
    peer.writer.transport.abort()
    await _until_detached(server, 0)
    assert sid in server.live
    live.streaming = False
    server.subagents.handles.pop("sa-1", None)


async def test_an_empty_session_stays_while_another_client_is_still_attached(daemon, tmp_path):
    server, sock = daemon
    first, sid = await _peer_on_new_session(sock, tmp_path)
    second = await Peer.connect(sock)
    activated = await second.send("session.activate", {"session_id": sid})
    await second.until(lambda f: f.get("id") == activated)
    first.writer.close()
    await _until_detached(server, 1)
    assert sid in server.live
    second.writer.close()
    await _until_detached(server, 0)
    await _until_not_live(server, sid)
