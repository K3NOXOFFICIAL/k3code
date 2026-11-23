"""k3d.ping / k3d.version: the gateway forwards each to the k3d sidecar socket with its token, answers
K3D_UNAVAILABLE (data.code ``k3d_unavailable``) when k3d cannot answer, needs the gateway token on a socket, and never
logs k3d's token. The fake k3d below follows ``rust/crates/k3d/src/server.rs``: nothing before ``gateway.auth``."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path

import pytest

from k3code.gateway import auth as gw_auth
from k3code.gateway import k3d as k3d_client
from k3code.gateway.protocol import K3D_UNAVAILABLE, UNAUTHORIZED
from m1cmd_helpers import make_server, rpc
from test_gateway_socket import Peer

TOKEN = "f" * 32 + "0" * 32  # a k3d-shaped token, distinctive enough to search logs for
VERSION = {"name": "k3d", "version": "9.9.9-fake"}


class FakeK3d:
    """A k3d on ``sock``: refuses methods until ``gateway.auth`` carries ``token``; records what it was sent."""

    def __init__(self, sock: Path, token: str = TOKEN) -> None:
        self.sock, self.token, self.seen = sock, token, []
        self.server: asyncio.base_events.Server | None = None

    async def __aenter__(self) -> FakeK3d:
        self.sock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        gw_auth.token_path(self.sock).write_text(TOKEN)
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.sock))
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        authed = False
        while line := await reader.readline():
            req = json.loads(line)
            self.seen.append(req)
            rid, method = req["id"], req["method"]
            if method == gw_auth.AUTH_METHOD:
                authed = req["params"].get("token") == self.token
                reply = {"result": {"ok": True}} if authed else {"error": {"code": UNAUTHORIZED, "message": "wrong"}}
            elif not authed:
                reply = {"error": {"code": UNAUTHORIZED, "message": f"{method} needs an authenticated connection"}}
            else:
                reply = {"result": VERSION if method == "k3d.version" else {"ok": True}}
            writer.write((json.dumps({"jsonrpc": "2.0", "id": rid, **reply}) + "\n").encode())
            await writer.drain()
        writer.close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv, _ = make_server(tmp_path, monkeypatch, [])
    return srv


async def test_ping_and_version_are_forwarded_after_auth(server):
    async with FakeK3d(k3d_client.socket_path()) as k3d:
        assert (await rpc(server, "k3d.ping"))["result"] == {"ok": True}
        assert (await rpc(server, "k3d.version"))["result"] == VERSION
    assert [r["method"] for r in k3d.seen] == ["gateway.auth", "k3d.ping", "gateway.auth", "k3d.version"]
    assert k3d.seen[0]["params"] == {"token": TOKEN}


async def test_no_socket_is_k3d_unavailable_and_the_gateway_stays_up(server):
    assert not k3d_client.socket_path().exists()
    err = (await rpc(server, "k3d.ping"))["error"]
    assert err["code"] == K3D_UNAVAILABLE and err["data"] == {"code": "k3d_unavailable"}
    assert err["message"].startswith("k3d_unavailable")
    assert "result" in await rpc(server, "system.battery")  # the next request is still answered


async def test_a_stale_socket_that_refuses_is_k3d_unavailable(server):
    sock = k3d_client.socket_path()
    sock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(sock))  # bound, never listening: connect() is refused
    try:
        err = (await rpc(server, "k3d.version"))["error"]
    finally:
        stale.close()
    assert err["code"] == K3D_UNAVAILABLE and err["data"]["code"] == "k3d_unavailable"


async def test_a_refused_token_is_k3d_unavailable_and_never_logged(server, caplog):
    caplog.set_level(logging.DEBUG)
    async with FakeK3d(k3d_client.socket_path(), token="1" * 64):  # the token file holds TOKEN, k3d wants another
        err = (await rpc(server, "k3d.ping"))["error"]
    assert err["code"] == K3D_UNAVAILABLE and err["data"]["code"] == "k3d_unavailable"
    async with FakeK3d(k3d_client.socket_path()):
        assert (await rpc(server, "k3d.ping"))["result"] == {"ok": True}
    assert caplog.records, "the refusal should be logged"
    assert TOKEN not in caplog.text
    assert not [f for f in server._frames if TOKEN in f]


async def test_k3d_methods_need_the_gateway_token_on_a_socket(server, tmp_path):
    sock = tmp_path / "run" / "gw.sock"
    await server.start_socket(sock)
    server._running = True
    try:
        async with FakeK3d(k3d_client.socket_path()) as k3d:
            stranger = await Peer.connect(sock, auth=False)
            for method in ("k3d.ping", "k3d.version"):
                rid = await stranger.send(method, {})
                reply = await stranger.until(lambda f, rid=rid: f.get("id") == rid and "method" not in f)
                assert reply["error"]["code"] == UNAUTHORIZED, (method, reply)
            assert k3d.seen == []  # nothing was forwarded for the unauthenticated peer
            owner = await Peer.connect(sock)
            rid = await owner.send("k3d.version", {})
            reply = await owner.until(lambda f: f.get("id") == rid and "method" not in f)
            assert reply["result"] == VERSION
    finally:
        await server.stop_socket()
        await server.close()
