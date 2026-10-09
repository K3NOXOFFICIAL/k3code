"""M2-ops step 2: daemon over a Unix socket, watchdog pings, restart-storm safe mode, service unit."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path

import pytest

from daemon_helpers import _stop_daemon
from k3code import daemon, sdnotify, service


def _write_fake_config(home: Path, script: list[dict]) -> None:
    (home).mkdir(parents=True, exist_ok=True)
    (home / "fake.json").write_text(json.dumps(script))
    (home / "config.yaml").write_text(
        "providers:\n  - {name: fake, kind: openai, base_url: 'http://fake', api_key_env: FAKE_KEY,"
        " models: {default: m}}\nreliability: {flags: {netwatch: false}}\n"
    )


class Peer:
    """A minimal JSON-RPC client over the daemon socket."""

    CALL_DEADLINE_S = 30.0  # wall-clock cap for one call(); events must not keep a missing response waiting forever

    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.events: list[dict] = []
        self._n = 0

    @classmethod
    async def connect(cls, path: Path) -> Peer:
        r, w = await asyncio.open_unix_connection(str(path))
        return cls(r, w)

    async def call(self, method: str, **params):
        self._n += 1
        rid = self._n
        self.w.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
        await self.w.drain()
        deadline = time.monotonic() + self.CALL_DEADLINE_S
        seen: list[str] = []
        try:
            while (left := deadline - time.monotonic()) > 0:
                frame = json.loads(await asyncio.wait_for(self.r.readline(), min(10, left)))
                if frame.get("id") == rid:
                    return frame
                if frame.get("method") == "event":
                    self.events.append(frame["params"])
                    seen.append(str(frame["params"].get("type")))
                else:
                    seen.append(str(frame.get("method") or "response"))
        except TimeoutError:
            pass
        raise AssertionError(f"no response to {method!r} within {self.CALL_DEADLINE_S}s; last frames: {seen[-5:]}")

    async def read_until(self, etype: str, timeout: float = 10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for e in self.events:
                if e["type"] == etype:
                    return e
            frame = json.loads(await asyncio.wait_for(self.r.readline(), timeout))
            if frame.get("method") == "event":
                self.events.append(frame["params"])
        raise AssertionError(f"no {etype} event; got {[e['type'] for e in self.events]}")

    def close(self):
        self.w.close()


class _EventsOnlyReader:
    """Answers every read with an event frame, never with a response."""

    async def readline(self):
        await asyncio.sleep(0.01)
        return b'{"jsonrpc": "2.0", "method": "event", "params": {"type": "tick"}}\n'


class _NullWriter:
    def write(self, data):
        pass

    async def drain(self):
        pass


async def test_call_fails_instead_of_hanging_when_only_events_arrive(monkeypatch):
    monkeypatch.setattr(Peer, "CALL_DEADLINE_S", 0.2)
    peer = Peer(_EventsOnlyReader(), _NullWriter())
    with pytest.raises(AssertionError, match=r"'session.list'.*\['tick'"):
        await asyncio.wait_for(peer.call("session.list"), 5)
    assert peer.events


@pytest.fixture
async def running_daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_fake_config(
        home,
        [{"type": "text", "text": "background done"}, {"type": "usage", "prompt_tokens": 7, "completion_tokens": 3}],
    )
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(home / "fake.json"))
    monkeypatch.setenv("FAKE_KEY", "x")
    monkeypatch.delenv("K3CODE_GATEWAY_SOCKET", raising=False)
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    ready, holder = asyncio.Event(), []
    task = asyncio.create_task(
        daemon.run_daemon(
            home=home, install_signals=False, ready_event=ready, server_out=holder, watchdog_interval=0.05
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 10)
        yield home, holder[0]
    finally:
        await _stop_daemon(holder[0] if holder else None, task)


async def test_two_clients_share_one_session_and_background_prompt_survives_detach(running_daemon):
    home, server = running_daemon
    sock = daemon.socket_path(home)
    a = await Peer.connect(sock)
    await a.read_until("gateway.ready")
    await a.read_until("session.active_list")  # attach snapshot
    created = await a.call("session.create", cwd=str(home), background=True)
    sid = created["result"]["session_id"]
    assert created["result"]["info"]["background"] is True
    await a.call("prompt.submit", text="work in the background")
    a.close()  # detach while (or just before) the turn runs

    b = await Peer.connect(sock)
    snap = await b.read_until("session.active_list")
    assert any(r["id"] == sid for r in snap["payload"]["sessions"])
    # Attach b to the same session; it is the same live object, not a copy.
    resumed = await b.call("session.resume", session_id=sid)
    assert resumed["result"]["session_id"] == sid
    assert server.live[sid] is server.live_for(server.store.get(sid))
    for _ in range(100):
        rows = (await b.call("session.list"))["result"]["sessions"]
        row = next(r for r in rows if r["id"] == sid)
        if row["message_count"] >= 2 and row["state"] == "completed":  # background run finished
            break
        await asyncio.sleep(0.1)
    assert row["message_count"] >= 2, row
    active = (await b.call("session.active_list"))["result"]["sessions"]
    assert active[0]["id"] == sid and active[0]["state"] == "completed"
    stats = server.usage.aggregate("session")
    assert stats and stats[0]["calls"] == 1 and stats[0]["tokens_in"] == 7
    b.close()


async def test_events_reach_every_client_on_the_session(running_daemon):
    home, server = running_daemon
    sock = daemon.socket_path(home)
    a, b = await Peer.connect(sock), await Peer.connect(sock)
    sid = (await a.call("session.create", cwd=str(home)))["result"]["session_id"]
    await b.call("session.resume", session_id=sid)
    await a.call("prompt.submit", text="hi")
    done_a = await a.read_until("message.complete")
    done_b = await b.read_until("message.complete")
    assert done_a["payload"]["text"] == done_b["payload"]["text"] == "background done"
    a.close()
    b.close()


async def test_safe_mode_blocks_background_prompts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_fake_config(home, [{"type": "text", "text": "x"}])
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(home / "fake.json"))
    monkeypatch.setenv("FAKE_KEY", "x")
    now = time.time()
    daemon.restarts_file(home).parent.mkdir(parents=True, exist_ok=True)
    daemon.restarts_file(home).write_text(json.dumps([now - i for i in range(1, 6)]))  # 5 recent + this = 6
    ready, holder = asyncio.Event(), []
    task = asyncio.create_task(
        daemon.run_daemon(home=home, install_signals=False, ready_event=ready, server_out=holder, watchdog_interval=5)
    )
    await asyncio.wait_for(ready.wait(), 10)
    server = holder[0]
    assert server.background_paused
    peer = await Peer.connect(daemon.socket_path(home))
    note = await peer.read_until("notification.show")
    assert "background work is paused" in note["payload"]["text"] and note["importance"] == "essential"
    sid = (await peer.call("session.create", cwd=str(home)))["result"]["session_id"]
    resp = await peer.call("prompt.submit", text="x", background=True)
    assert "error" in resp and "paused" in resp["error"]["message"]
    resumed = await peer.call("command.dispatch", name="daemon", arg="resume", session_id=sid)
    assert "resumed" in resumed["result"]["message"] and not server.background_paused
    peer.close()
    server.request_stop()
    await asyncio.wait_for(task, 10)


def test_restart_counter_prunes_old_and_flags_storm(tmp_path):
    now = 10_000.0
    assert daemon.record_restart(tmp_path, now=now - 5000) == 1  # old: pruned next time
    counts = [daemon.record_restart(tmp_path, now=now + i) for i in range(6)]
    assert counts == [1, 2, 3, 4, 5, 6]  # the 5000-s-old stamp is gone
    assert [daemon.storm_active(c) for c in counts] == [False] * 5 + [True]


def test_socket_path_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_GATEWAY_SOCKET", str(tmp_path / "x.sock"))
    assert daemon.socket_path(tmp_path) == tmp_path / "x.sock"
    monkeypatch.delenv("K3CODE_GATEWAY_SOCKET")
    monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", f"unix://{tmp_path}/h.sock")
    assert daemon.socket_path(tmp_path) == tmp_path / "h.sock"


async def test_watchdog_and_ready_pings_reach_notify_socket(tmp_path, monkeypatch):
    path = tmp_path / "notify.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(path))
    srv.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    assert sdnotify.ready()
    task = asyncio.create_task(sdnotify.watchdog_loop(0.05))
    await asyncio.sleep(0.3)
    task.cancel()
    msgs = []
    srv.setblocking(False)
    while True:
        try:
            msgs.append(srv.recv(64).decode())
        except BlockingIOError:
            break
    srv.close()
    assert msgs[0] == "READY=1"
    assert msgs.count("WATCHDOG=1") >= 3


def test_notify_abstract_socket_and_no_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.notify("READY=1") is False
    monkeypatch.setenv("WATCHDOG_USEC", "120000000")
    assert sdnotify.watchdog_interval(30.0) == 30.0
    monkeypatch.setenv("WATCHDOG_USEC", "20000000")
    assert sdnotify.watchdog_interval(30.0) == 10.0
    abstract = "k3test-" + str(os.getpid())
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind("\0" + abstract)
    srv.settimeout(2)
    assert sdnotify.notify("WATCHDOG=1", {"NOTIFY_SOCKET": "@" + abstract})
    assert srv.recv(64) == b"WATCHDOG=1"
    srv.close()


def test_unit_file_matches_policy_and_repo_copy():
    unit = service.render_unit("/usr/bin/k3code daemon")
    unit_section, service_section = unit.split("[Service]")
    for line in (
        f"StartLimitIntervalSec={service.START_LIMIT_INTERVAL_S}",
        f"StartLimitBurst={service.START_LIMIT_BURST}",
        "OnFailure=k3code-recover.service",
    ):
        assert line in unit_section  # belongs in [Unit], not [Service]
    for line in (
        "Type=notify",
        "NotifyAccess=main",
        "Restart=always",
        "RestartSec=5",
        "WatchdogSec=120",
        "Nice=5",
        "IOSchedulingClass=idle",
        "MemoryHigh=2G",
        "MemoryMax=4G",
        "TasksMax=512",
        "LimitNOFILE=65536",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "ExecStart=/usr/bin/k3code daemon",
    ):
        assert line in service_section
    # the user manager has no network-online.target, so ordering on it would be a no-op
    assert "network-online" not in "\n".join(ln for ln in unit.splitlines() if not ln.startswith("#"))
    # the daemon runs bubblewrap and the user's tools: no filesystem/namespace sandboxing of the daemon itself
    for forbidden in ("ProtectHome", "ProtectSystem", "PrivateTmp", "PrivateNetwork"):
        assert forbidden not in "\n".join(ln for ln in unit.splitlines() if not ln.startswith("#"))
    repo = Path(__file__).resolve().parents[2] / "install" / "systemd" / "k3code.service"
    assert repo.read_text() == service.render_unit(service.DEFAULT_EXEC_START)


def test_unit_exec_start_uses_the_stable_current_symlink_and_pins_non_default_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("K3CODE_DATA", raising=False)
    monkeypatch.delenv("K3CODE_HOME", raising=False)
    default_data = tmp_path / "home" / ".local" / "share" / "k3code"
    exe = default_data / "current" / "venv" / "bin" / "k3code"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    unit = service.render_unit()
    assert f"ExecStart={exe} daemon\n" in unit  # not a versions/<ver> path and not whatever is on PATH
    assert "K3CODE_DATA" not in unit and "K3CODE_HOME" not in unit  # defaults need no Environment= lines
    custom = tmp_path / "other data"
    (custom / "current" / "venv" / "bin").mkdir(parents=True)
    (custom / "current" / "venv" / "bin" / "k3code").write_text("#!/bin/sh\n")
    monkeypatch.setenv("K3CODE_DATA", str(custom))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "kh"))
    unit = service.render_unit()
    assert f'ExecStart="{custom}/current/venv/bin/k3code" daemon\n' in unit  # a space in the path is quoted
    assert f'Environment="K3CODE_DATA={custom}"\n' in unit
    assert f'Environment="K3CODE_HOME={tmp_path / "kh"}"\n' in unit
    assert "K3CODE_DATA" not in service.render_unit("/usr/bin/k3code daemon")  # an explicit ExecStart is verbatim


def test_service_install_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    out = "\n".join(service.install(dry_run=True))
    assert "Type=notify" in out and "enable --now" in out and "loginctl enable-linger" in out
    assert not (tmp_path / "cfg").exists()
    assert "disable --now" in "\n".join(service.uninstall(dry_run=True))


async def test_gateway_attach_bridge_pumps_stdio_to_the_socket(running_daemon):
    import sys

    home, _server = running_daemon
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "k3code.cli",
        "gateway",
        "--attach",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "K3CODE_GATEWAY_SOCKET": str(daemon.socket_path(home))},
    )
    try:
        proc.stdin.write(b'{"jsonrpc":"2.0","id":9,"method":"session.list","params":{}}\n')
        await proc.stdin.drain()
        types, result = [], None
        while result is None:
            frame = json.loads(await asyncio.wait_for(proc.stdout.readline(), 15))
            if frame.get("id") == 9:
                result = frame["result"]
            else:
                types.append(frame["params"]["type"])
        assert types[:2] == ["gateway.ready", "session.active_list"] and "sessions" in result
    finally:
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 10)


async def test_readonly_attach_outside_a_pane_still_refuses_changes(running_daemon):
    """The read-only filter lived in PaneLink only: outside a k3 pane stdin went to the daemon unfiltered."""
    import sys

    home, _server = running_daemon
    env = {k: v for k, v in os.environ.items() if not k.startswith("TUIOS_")}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "k3code.cli",
        "gateway",
        "--attach",
        "--readonly",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**env, "K3CODE_GATEWAY_SOCKET": str(daemon.socket_path(home))},
    )
    try:
        proc.stdin.write(b'{"jsonrpc":"2.0","id":8,"method":"session.delete","params":{"session_id":"x"}}\n')
        proc.stdin.write(b'{"jsonrpc":"2.0","id":9,"method":"session.list","params":{}}\n')
        await proc.stdin.drain()
        got: dict = {}
        while 9 not in got:
            frame = json.loads(await asyncio.wait_for(proc.stdout.readline(), 15))
            if frame.get("id") in (8, 9):
                got[frame["id"]] = frame
        assert got[8]["error"]["message"] == "this pane is read-only"
        assert "sessions" in got[9]["result"]  # reads still go through
    finally:
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 10)


async def test_gateway_attach_without_daemon_fails_clearly(tmp_path):
    import sys

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "k3code.cli",
        "gateway",
        "--attach",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "K3CODE_GATEWAY_SOCKET": str(tmp_path / "none.sock")},
    )
    _out, err = await asyncio.wait_for(proc.communicate(), 15)
    assert proc.returncode == 1 and b"Start it with `k3code daemon`" in err


async def test_in_flight_turn_survives_client_disconnect(tmp_path, monkeypatch):
    """The turn is still inside a slow bash call when the only client goes away."""
    home = tmp_path / "home"
    _write_fake_config(
        home,
        [
            {
                "type": "tool_call",
                "id": "c1",
                "name": "bash",
                "when": "first",
                "arguments": {"command": "sleep 1.5; echo slept > marker.txt"},
            },
            {"type": "text", "text": "all done", "when": "after_tool"},
        ],
    )
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(home / "fake.json"))
    monkeypatch.setenv("FAKE_KEY", "x")
    ready, holder = asyncio.Event(), []
    task = asyncio.create_task(
        daemon.run_daemon(home=home, install_signals=False, ready_event=ready, server_out=holder, watchdog_interval=5)
    )
    await asyncio.wait_for(ready.wait(), 10)
    server = holder[0]
    sock = daemon.socket_path(home)
    work = tmp_path / "work"
    work.mkdir()
    a = await Peer.connect(sock)
    sid = (await a.call("session.create", cwd=str(work), background=True))["result"]["session_id"]
    await a.call("session.mode.set", mode="yolo")  # no approval prompt; bash runs in the sandbox when usable
    await a.call("prompt.submit", text="run the slow command")
    await a.read_until("tool.start")
    assert server.live[sid].state == "working" and not (work / "marker.txt").exists()
    a.close()  # detach mid-turn
    b = await Peer.connect(sock)
    await b.call("session.resume", session_id=sid)
    assert server.live[sid].state == "working"  # still running with nobody attached
    for _ in range(100):
        row = next(r for r in (await b.call("session.list"))["result"]["sessions"] if r["id"] == sid)
        if row["state"] == "completed" and row["message_count"] >= 4:
            break
        await asyncio.sleep(0.1)
    assert row["state"] == "completed" and row["message_count"] >= 4, row
    assert (work / "marker.txt").read_text().strip() == "slept"
    b.close()
    server.request_stop()
    await asyncio.wait_for(task, 10)


# ── regressions from the long-run audit: one daemon per home ──


def test_a_second_daemon_on_the_same_home_is_refused(tmp_path):
    """Nothing stopped a second `k3code daemon`: it unlinked the live socket and rebound it, leaving the first daemon
    (sessions, cron, loops) running but unreachable."""
    import os

    from k3code.daemon import DaemonAlreadyRunning, acquire_instance_lock

    sock = tmp_path / "run" / "gateway.sock"
    first = acquire_instance_lock(sock)
    try:
        with pytest.raises(DaemonAlreadyRunning):
            acquire_instance_lock(sock)
        assert (sock.parent / "daemon.lock").read_text().strip() == str(os.getpid())
    finally:
        os.close(first)
    os.close(acquire_instance_lock(sock))  # released: a new daemon can start


async def test_start_socket_refuses_a_live_socket_and_stop_leaves_foreign_sockets_alone(tmp_path, monkeypatch):
    import contextlib

    from m1cmd_helpers import make_server

    one, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sock = tmp_path / "run" / "gateway.sock"
    await one.start_socket(sock)
    two, _ = make_server(tmp_path / "other", monkeypatch, ["ok"])
    with pytest.raises(RuntimeError, match="another process"):
        await two.start_socket(sock)
    assert sock.exists()  # the live one was not unlinked
    # a socket that was replaced by someone else is not ours to remove on stop
    sock.unlink()
    replacement = await asyncio.start_unix_server(lambda r, w: None, path=str(sock))
    await one.stop_socket()
    assert sock.exists()
    replacement.close()
    with contextlib.suppress(Exception):
        await one.close()
        await two.close()
