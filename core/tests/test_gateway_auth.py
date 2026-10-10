"""Gateway hardening: the daemon token, the private run dir, frame and concurrency caps, config.set validation, the
child environment, and `k3code slash` refusing to answer questions for the user."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from k3code.bundle import write_bundle
from k3code.gateway import auth as gw_auth
from k3code.gateway.protocol import TOO_MANY_REQUESTS, UNAUTHORIZED
from k3code.permissions import PermissionMode
from m1cmd_helpers import make_server, new_session, rpc
from test_gateway_socket import Peer


@pytest.fixture
async def gw(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sock = tmp_path / "run" / "gw.sock"
    await server.start_socket(sock)
    server._running = True
    yield server, sock
    await server.stop_socket()
    await server.close()


async def _call(peer: Peer, method: str, params: dict) -> dict:
    rid = await peer.send(method, params)
    return await peer.until(lambda f: f.get("id") == rid and "method" not in f)


async def test_privileged_methods_need_the_daemon_token(gw, tmp_path):
    """Any same-user process (an MCP server, an approved bash command) could connect and run shell.exec unsandboxed or
    switch a session to YOLO: the 0600 socket was the only check."""
    server, sock = gw
    sid = await new_session(server, tmp_path)
    marker = tmp_path / "ran"
    stranger = await Peer.connect(sock, auth=False)
    shell = await _call(stranger, "shell.exec", {"command": f"touch {marker}", "session_id": sid})
    assert shell["error"]["code"] == UNAUTHORIZED and not marker.exists()
    for method, params in (
        ("config.set", {"key": "yolo", "session_id": sid}),
        ("session.mode.set", {"mode": "yolo", "session_id": sid}),
        ("session.mode.cycle", {"session_id": sid}),
        ("slash.exec", {"command": "permissions yolo", "session_id": sid}),
        ("command.dispatch", {"name": "permissions", "arg": "yolo", "session_id": sid}),
        ("session.workspace.move", {"path": str(tmp_path)}),
        ("reload.mcp", {}),
        ("browser.manage", {"action": "connect", "url": "http://127.0.0.1:9"}),
    ):
        reply = await _call(stranger, method, params)
        assert reply.get("error", {}).get("code") == UNAUTHORIZED, (method, reply)
    assert server.live[sid].perms.mode != PermissionMode.YOLO
    status = await _call(stranger, "browser.manage", {"action": "status"})  # read-only calls stay open
    assert "result" in status
    wrong = await _call(stranger, gw_auth.AUTH_METHOD, {"token": "0" * 64})
    assert wrong["error"]["code"] == UNAUTHORIZED
    assert (await _call(stranger, "session.mode.set", {"mode": "yolo", "session_id": sid}))["error"]

    owner = await Peer.connect(sock)  # sends gateway.auth with the token from the run dir
    shell = await _call(owner, "shell.exec", {"command": f"touch {marker}", "session_id": sid})
    assert shell["result"]["code"] == 0 and marker.exists()
    mode = await _call(owner, "session.mode.set", {"mode": "yolo", "session_id": sid})
    assert mode["result"]["mode"] == "yolo" and server.live[sid].perms.mode == PermissionMode.YOLO


async def test_an_unauthenticated_peer_cannot_answer_a_pending_request(gw, tmp_path):
    server, sock = gw
    fut = asyncio.get_running_loop().create_future()
    server._server_request_futures["approval-1"] = fut
    stranger = await Peer.connect(sock, auth=False)
    answer = {"jsonrpc": "2.0", "id": "approval-1", "result": {"choice": "once"}}
    stranger.writer.write((json.dumps(answer) + "\n").encode())
    probe = await _call(stranger, "session.most_recent", {})  # handled after the answer frame
    assert "result" in probe and not fut.done()
    owner = await Peer.connect(sock)
    owner.writer.write((json.dumps(answer) + "\n").encode())
    assert (await asyncio.wait_for(fut, 5)) == {"choice": "once"}


async def test_run_dir_is_private_and_the_token_file_0600(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    run = tmp_path / "run"
    run.mkdir(mode=0o755)
    run.chmod(0o755)  # an existing run dir from an older daemon, created with the default umask
    sock = run / "gw.sock"
    await server.start_socket(sock)
    try:
        assert stat.S_IMODE(run.stat().st_mode) == 0o700
        token = gw_auth.token_path(sock)
        assert stat.S_IMODE(token.stat().st_mode) == 0o600 and len(token.read_text()) == 64
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
    finally:
        await server.stop_socket()
        await server.close()
    assert not gw_auth.token_path(sock).exists()  # removed with the socket


async def test_a_custom_socket_directory_is_never_chmodded(tmp_path, monkeypatch):
    """K3CODE_GATEWAY_SOCKET=~/k3.sock used to chmod $HOME to 0700 (and /tmp was refused as foreign-owned)."""
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    custom = tmp_path / "custom"
    custom.mkdir()
    custom.chmod(0o755)
    sock = custom / "k3.sock"
    await server.start_socket(sock)
    try:
        assert stat.S_IMODE(custom.stat().st_mode) == 0o755
        assert stat.S_IMODE(gw_auth.token_path(sock).stat().st_mode) == 0o600
        assert gw_auth.token_path(sock) == custom / "k3.token"
    finally:
        await server.stop_socket()
        await server.close()
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)  # group/other-writable, no sticky bit: another user could swap the socket or token
    with pytest.raises(RuntimeError, match="writable by other users"):
        await server.start_socket(shared / "k3.sock")
    shared.chmod(0o1777)  # /tmp style: allowed
    await server.start_socket(shared / "k3.sock")
    await server.stop_socket()
    await server.close()
    assert stat.S_IMODE(shared.stat().st_mode) == 0o1777


async def test_the_socket_is_never_bound_with_group_or_other_access(tmp_path, monkeypatch):
    """chmod 0600 after bind left a window in which the socket had the umask mode (0755 under umask 022)."""
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    real_start = asyncio.start_unix_server
    born: list[int] = []

    async def spy(*args, path, **kwargs):  # the mode the socket had before start_socket's chmod
        srv = await real_start(*args, path=path, **kwargs)
        born.append(stat.S_IMODE(os.stat(path).st_mode))
        return srv

    monkeypatch.setattr(asyncio, "start_unix_server", spy)
    old_umask = os.umask(0o022)
    try:
        await server.start_socket(tmp_path / "run" / "gw.sock")
    finally:
        os.umask(old_umask)
        await server.stop_socket()
        await server.close()
    assert len(born) == 1 and born[0] & 0o077 == 0, oct(born[0])  # umask 077: born 0700, chmod'ed to 0600 next


async def test_daemon_run_dir_is_created_private(tmp_path, monkeypatch):
    from k3code import daemon

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "fresh"))
    fd = daemon.acquire_instance_lock(tmp_path / "fresh" / "run" / "gateway.sock")
    os.close(fd)
    assert stat.S_IMODE((tmp_path / "fresh" / "run").stat().st_mode) == 0o700


def test_child_env_never_names_the_daemon_socket(monkeypatch):
    from k3code.mcpclient import stdio_env
    from k3code.providers.claude_cli import _clean_env
    from k3code.reliability.sandbox import child_env

    monkeypatch.setenv("K3CODE_GATEWAY_SOCKET", "/home/user/.myapp/run/gateway.sock")
    monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", "unix:///home/user/.myapp/run/gateway.sock")
    leaked = {"K3CODE_GATEWAY_SOCKET": "/x.sock", "HERMES_TUI_GATEWAY_URL": "unix:///x.sock", "KEEP": "1"}
    for env in (child_env(leaked), stdio_env(leaked)):  # an MCP server's configured env cannot re-add them
        assert env.get("KEEP") == "1"
        assert "K3CODE_GATEWAY_SOCKET" not in env and "HERMES_TUI_GATEWAY_URL" not in env
    claude = _clean_env()
    assert "K3CODE_GATEWAY_SOCKET" not in claude and "HERMES_TUI_GATEWAY_URL" not in claude


@pytest.mark.parametrize("module", ["k3code.commands.branch", "k3code.commands.review"])
async def test_slash_command_git_gets_the_scrubbed_child_env(tmp_path, monkeypatch, module):
    """The daemon scrubs its own environment, but the TUI-spawned stdio gateway does not: /branch and /review ran git
    with the full environment, the daemon socket and the provider keys included."""
    import importlib

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_git = bindir / "git"
    fake_git.write_text("#!/bin/sh\nenv\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("K3CODE_GATEWAY_SOCKET", "/home/user/.myapp/run/gateway.sock")
    monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", "unix:///home/user/.myapp/run/gateway.sock")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-alice")
    rc, out = await importlib.import_module(module)._git(tmp_path, "status")
    names = {line.split("=", 1)[0] for line in out.splitlines()}
    assert rc == 0 and "PATH" in names  # the fake git ran
    assert not names & {"K3CODE_GATEWAY_SOCKET", "HERMES_TUI_GATEWAY_URL", "OMNIROUTE_API_KEY"}, names


async def test_the_daemon_drops_the_socket_variables_from_its_own_environment(tmp_path, monkeypatch):
    """Children that get the daemon's whole environment (git in /branch, the claude CLI) must not inherit them."""
    from daemon_helpers import _stop_daemon
    from k3code import daemon

    home = tmp_path / "home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    sock = tmp_path / "custom" / "gw.sock"
    monkeypatch.setenv("K3CODE_GATEWAY_SOCKET", str(sock))
    ready, holder = asyncio.Event(), []
    task = asyncio.create_task(
        daemon.run_daemon(home=home, install_signals=False, ready_event=ready, server_out=holder, watchdog_interval=5)
    )
    try:
        await asyncio.wait_for(ready.wait(), 10)
        assert holder[0].socket_path == sock  # the variable was honoured before it was dropped
        assert "K3CODE_GATEWAY_SOCKET" not in os.environ
    finally:
        await _stop_daemon(holder[0] if holder else None, task)


async def test_an_over_limit_frame_is_refused_and_its_tail_is_not_parsed(gw):
    """A 64 MiB line limit, each line parsed twice: a few big frames held GBs. Now 8 MiB, with a clear error."""
    _, sock = gw
    peer = await Peer.connect(sock)
    await peer.send("no.such.method", {"pad": "x" * (9 << 20)})
    err = await peer.until(lambda f: "error" in f and f.get("id") is None, timeout=30)
    assert "longer than 8 MiB" in err["error"]["message"]
    after = await peer.send("no.such.method", {})
    reply = await peer.until(lambda f: "error" in f, timeout=30)  # the next error is this one, not a parse error
    assert reply.get("id") == after, reply


async def test_concurrent_slash_requests_per_connection_are_capped(gw, tmp_path):
    """command.dispatch / slash.exec run as tasks; one connection could start any number of them."""
    from k3code.gateway import server as gateway_server

    server, sock = gw
    sid = await new_session(server, tmp_path)
    bundle = tmp_path / "x.k3bundle"
    write_bundle(bundle, store=server.store, cwd=tmp_path, session_ids=[sid], settings=False)
    peer = await Peer.connect(sock)
    await _call(peer, "session.create", {"cwd": str(tmp_path)})  # clarify goes to the clients of that session
    cap = gateway_server.MAX_CONCURRENT_PER_CLIENT
    for _ in range(cap):  # each waits for our answer to its clarify
        await peer.send("slash.exec", {"command": f"import {bundle}", "session_id": None})
    for _ in range(cap):
        await peer.until(lambda f: f.get("method") == "clarify")
    extra = await peer.send("slash.exec", {"command": "help", "session_id": None})
    refused = await peer.until(lambda f: f.get("id") == extra)
    assert refused["error"]["code"] == TOO_MANY_REQUESTS


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("goal.max_turns", "lots"),  # wrong type
        ("permissions.update", {"x": 1}),  # a dict section's method
        ("mcp.servers", {}),  # not settable at runtime
        ("display.no_such_field", "1"),
    ],
)
async def test_config_set_refuses_unknown_keys_and_bad_values(tmp_path, monkeypatch, key, value):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    before = server.config.model_dump()
    reply = await rpc(server, "config.set", {"key": key, "value": value})
    assert key in reply["error"]["message"], reply
    assert server.config.model_dump() == before
    await server.close()


async def test_config_set_validates_and_coerces_an_allowed_key(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    assert (await rpc(server, "config.set", {"key": "goal.max_turns", "value": "12"}))["result"]["value"] == 12
    assert server.config.goal.max_turns == 12
    await server.close()


async def test_config_set_goal_max_turns_zero_means_no_turn_limit(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sid = await new_session(server, tmp_path)
    live = server._session_for(sid)
    assert (await rpc(server, "config.set", {"key": "goal.max_turns", "value": 12}))["result"]["value"] == 12
    assert server.goal_manager(live).set("capped").max_turns == 12
    reply = await rpc(server, "config.set", {"key": "goal.max_turns", "value": 0})
    assert reply["result"]["value"] == 0 and server.config.goal.max_turns == 0
    state = server.goal_manager(live).set("uncapped")
    assert state.max_turns == 0 and not state.budget_spent()
    state.turns_used = 500
    assert not state.budget_spent()  # no cap however long it runs
    await server.close()


async def test_config_set_goal_gate_max_retries_reaches_new_goals(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sid = await new_session(server, tmp_path)
    live = server._session_for(sid)
    assert server.goal_manager(live).set("default", check="true").gates[0].max_retries == 20
    reply = await rpc(server, "config.set", {"key": "goal.gate_max_retries", "value": 0})
    assert reply["result"]["value"] == 0 and server.config.goal.gate_max_retries == 0
    assert server.goal_manager(live).set("unlimited", check="true").gates[0].max_retries == 0
    await server.close()


async def test_k3code_slash_refuses_a_command_that_asks_a_question(gw, tmp_path):
    """slash_via_daemon answered every clarify with its first choice: whatever that did, it did unattended."""
    from k3code.daemon import slash_via_daemon

    server, sock = gw
    sid = await new_session(server, tmp_path)
    bundle = tmp_path / "x.k3bundle"
    write_bundle(bundle, store=server.store, cwd=tmp_path, session_ids=[sid], settings=False)
    sessions_before = len(server.store.list())
    out = await asyncio.wait_for(slash_via_daemon(f"/import {bundle}", cwd=str(tmp_path), sock=sock), 30)
    assert out.startswith("error:") and "cannot answer" in out
    assert len(server.store.list()) == sessions_before  # nothing was imported


async def test_k3code_slash_without_the_token_fails_closed(gw, tmp_path):
    from k3code.daemon import slash_via_daemon

    _, sock = gw
    gw_auth.token_path(sock).unlink()
    with pytest.raises(gw_auth.AuthFailed):
        await slash_via_daemon("/help", cwd=str(tmp_path), sock=sock)


def test_auth_token_lives_next_to_the_socket():
    assert gw_auth.token_path(Path("/home/user/.myapp/run/gateway.sock")) == Path("/home/user/.myapp/run/gateway.token")
