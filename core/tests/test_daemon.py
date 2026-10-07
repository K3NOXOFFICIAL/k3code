"""M2-ops step 2: daemon over a Unix socket, watchdog pings, restart-storm safe mode, service unit."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path

import pytest

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
        while True:
            frame = json.loads(await asyncio.wait_for(self.r.readline(), 10))
            if frame.get("id") == rid:
                return frame
            if frame.get("method") == "event":
                self.events.append(frame["params"])

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
    await asyncio.wait_for(ready.wait(), 10)
    yield home, holder[0]
    holder[0].request_stop()
    await asyncio.wait_for(task, 10)


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
        if row["message_count"] >= 2 and row["state"] == "idle":
            break
        await asyncio.sleep(0.1)
    assert row["message_count"] >= 2, row
    active = (await b.call("session.active_list"))["result"]["sessions"]
    assert active[0]["id"] == sid and active[0]["state"] == "idle"
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
    for line in ("StartLimitIntervalSec=600", "StartLimitBurst=20"):
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
        "ExecStart=/usr/bin/k3code daemon",
    ):
        assert line in service_section
    repo = Path(__file__).resolve().parents[2] / "install" / "systemd" / "k3code.service"
    assert repo.read_text() == service.render_unit(service.DEFAULT_EXEC_START)


def test_service_install_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    out = "\n".join(service.install(dry_run=True))
    assert "Type=notify" in out and "enable --now" in out and "loginctl enable-linger" in out
    assert not (tmp_path / "cfg").exists()
    assert "disable --now" in "\n".join(service.uninstall(dry_run=True))
