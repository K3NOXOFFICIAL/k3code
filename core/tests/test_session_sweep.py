"""The daemon-start sweep of old empty stored sessions: every `k3code agents` run, every `n` and every abandoned TUI
start leaves a 0-message row; hidden from the agent view, auto-resume and the switcher, they piled up for good."""

from __future__ import annotations

import asyncio
import time

import m1cmd_helpers as m1
from k3code import daemon
from k3code.automation.engine import AutomationEngine
from k3code.gateway.sessions import SessionStore
from test_daemon import _write_fake_config

DAY = 24 * 3600.0


def _age(store: SessionStore, sid: str, updated_at: float) -> None:
    """``save`` stamps ``updated_at`` with the clock: set it behind the store's back."""
    store._db.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (updated_at, sid))
    store._db.commit()


async def test_sweep_deletes_only_old_empty_unclaimed_rows_and_is_idempotent(tmp_path, monkeypatch):
    now = time.time()
    first, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    ids = {}
    for name in ("old_empty", "young_empty", "old_message", "old_title", "old_meta", "old_loop", "old_effort"):
        ids[name] = await m1.new_session(first, tmp_path)  # the real session.create path, like the TUI's
    first.live[ids["old_message"]].stored.messages.append({"role": "user", "content": "hi"})
    first.store.save(first.live[ids["old_message"]].stored)
    await m1.rpc(first, "session.title", {"session_id": ids["old_title"], "title": "keep me"})
    meta_row = first.live[ids["old_meta"]].stored
    meta_row.meta["add_dirs"] = ["/tmp/extra"]
    first.store.save(meta_row)
    effort_row = first.live[ids["old_effort"]].stored
    effort_row.meta["reasoning_effort"] = "high"
    first.store.save(effort_row)
    for name, sid in ids.items():
        _age(first.store, sid, now - (29 if name == "young_empty" else 31) * DAY)
    await first.close()

    # the daemon starts again on the same store: nothing above is live any more
    server, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    assert server.sweep_empty_sessions(now=now) == 0  # no automation engine yet: bound rows cannot be told apart
    assert server.store.get(ids["old_empty"]) is not None
    server.automation = AutomationEngine(server, use_netwatch=False)
    await server.automation.start()
    server.automation.loops.create(session_id=ids["old_loop"], prompt="check the build", interval="daily 09:00")
    live_sid = await m1.new_session(server, tmp_path)
    current = await m1.new_session(server, tmp_path)  # the client moves on: live_sid stays live, unattached
    assert live_sid in server.live and server.session.session_id == current
    _age(server.store, live_sid, now - 31 * DAY)
    _age(server.store, current, now - 31 * DAY)

    assert server.sweep_empty_sessions(now=now) == 1
    assert server.store.get(ids["old_empty"]) is None
    for kept in ("young_empty", "old_message", "old_title", "old_meta", "old_loop", "old_effort"):
        assert server.store.get(ids[kept]) is not None, kept
    assert server.store.get(live_sid) is not None and server.store.get(current) is not None
    assert server.sweep_empty_sessions(now=now) == 0  # idempotent

    # the loop ends: its empty row is an ordinary leftover from then on
    server.automation.loops.stop(session_id=ids["old_loop"])
    assert server.sweep_empty_sessions(now=now) == 1
    assert server.store.get(ids["old_loop"]) is None
    await server.close()


def test_store_sweep_honours_max_age_and_keep(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    a, b = store.create(), store.create()
    now = time.time()
    _age(store, a.session_id, now - 2 * DAY)
    _age(store, b.session_id, now - 2 * DAY)
    assert store.sweep_empty(now=now) == 0  # younger than the 30-day default
    assert store.sweep_empty(now=now, max_age=DAY, keep=lambda sid: sid == b.session_id) == 1
    assert store.get(a.session_id) is None and store.get(b.session_id) is not None


async def test_daemon_start_sweeps_old_empty_sessions(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_fake_config(home, [{"type": "text", "text": "x"}])
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(home / "fake.json"))
    monkeypatch.setenv("FAKE_KEY", "x")
    monkeypatch.delenv("K3CODE_GATEWAY_SOCKET", raising=False)
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    store = SessionStore(home / "sessions.db")
    old, kept = store.create(), store.create()
    kept.messages = [{"role": "user", "content": "hi"}]
    store.save(kept)
    for sid in (old.session_id, kept.session_id):
        _age(store, sid, time.time() - 40 * DAY)
    store.close()
    ready, holder = asyncio.Event(), []
    task = asyncio.create_task(
        daemon.run_daemon(home=home, install_signals=False, ready_event=ready, server_out=holder, watchdog_interval=5)
    )
    await asyncio.wait_for(ready.wait(), 10)
    server = holder[0]
    try:
        for _ in range(200):  # the sweep runs in the background after readiness
            if server.store.get(old.session_id) is None:
                break
            await asyncio.sleep(0.05)
        assert server.store.get(old.session_id) is None
        assert server.store.get(kept.session_id) is not None
    finally:
        server.request_stop()
        await asyncio.wait_for(task, 10)


async def test_sweep_keeps_rows_referenced_by_paused_automations(tmp_path, monkeypatch):
    now = time.time()
    server, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    server.automation = AutomationEngine(server, use_netwatch=False)
    await server.automation.start()
    ids = {n: await m1.new_session(server, tmp_path) for n in ("action", "trigger", "orphan", "orphan2")}
    server.automation.automations.add(
        name="a",
        trigger={"type": "cron", "schedule": "daily 09:00"},
        action={"type": "prompt", "prompt": "x", "session": ids["action"]},
    )
    server.automation.automations.add(
        name="t",
        trigger={"type": "session_event", "session": ids["trigger"], "event": "completed"},
        action={"type": "prompt", "prompt": "x"},
    )
    for ref in ("a", "t"):
        assert await server.automation.automations.pause(ref)
    server.live.clear()
    server.session = None
    for sid in ids.values():
        _age(server.store, sid, now - 31 * DAY)
    _age(server.store, ids["orphan2"], now)  # young for now: only the loop part below ages it
    assert not server.automation.bound_to(ids["action"])  # paused: not bound for close_live ...
    assert server.sweep_empty_sessions(now=now) == 1  # ... but the sweep still keeps it
    assert server.store.get(ids["action"]) is not None and server.store.get(ids["trigger"]) is not None
    assert server.store.get(ids["orphan"]) is None
    # a loop that is not running but not finished either (blocked on input) can be picked up again: its row stays
    loop = server.automation.loops.create(session_id=ids["orphan2"], prompt="p", interval="daily 09:00")
    server.automation.db.update("loops", loop["id"], state="blocked")
    _age(server.store, ids["orphan2"], now - 31 * DAY)
    assert server.sweep_empty_sessions(now=now) == 0
    assert server.store.get(ids["orphan2"]) is not None
    await server.close()


async def test_async_sweep_matches_sync_and_yields(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    now = time.time()
    rows = [store.create() for _ in range(450)]
    full = rows[0]
    full.messages = [{"role": "user", "content": "hi"}]
    store.save(full)
    for r in rows:
        _age(store, r.session_id, now - 40 * DAY)
    pinned = rows[1].session_id
    assert await store.sweep_empty_async(now=now, keep=lambda sid: sid == pinned) == 448
    assert {r.session_id for r in rows if store.get(r.session_id)} == {full.session_id, pinned}


class _SlowStore:
    """Wraps the real store: the async sweep blocks on an event until the test lets it go."""

    def __init__(self, inner, gate: asyncio.Event) -> None:
        self.inner, self.gate, self.started, self.finished = inner, gate, asyncio.Event(), False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def sweep_empty_async(self, **kw):
        self.started.set()
        await self.gate.wait()
        self.finished = True
        return await self.inner.sweep_empty_async(**kw)


def _daemon_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_fake_config(home, [{"type": "text", "text": "x"}])
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(home / "fake.json"))
    monkeypatch.setenv("FAKE_KEY", "x")
    monkeypatch.delenv("K3CODE_GATEWAY_SOCKET", raising=False)
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    return home


async def test_daemon_ready_before_sweep_finishes_and_shutdown_cancels_it(tmp_path, monkeypatch):
    from k3code.gateway.server import GatewayServer

    home = _daemon_env(tmp_path, monkeypatch)
    gate = asyncio.Event()  # never set: the sweep stays "slow" until shutdown
    slow: list[_SlowStore] = []
    orig = GatewayServer.sweep_empty_sessions_async

    async def patched(self, **kw):
        wrapped = _SlowStore(self.store, gate)
        slow.append(wrapped)
        real = self.store
        self.store = wrapped  # type: ignore[assignment]
        try:
            return await orig(self, **kw)
        finally:
            self.store = real

    monkeypatch.setattr(GatewayServer, "sweep_empty_sessions_async", patched)
    ready, holder = asyncio.Event(), []
    task = asyncio.create_task(
        daemon.run_daemon(home=home, install_signals=False, ready_event=ready, server_out=holder, watchdog_interval=5)
    )
    await asyncio.wait_for(ready.wait(), 10)  # readiness does not wait for the sweep
    for _ in range(200):
        if slow:
            break
        await asyncio.sleep(0.05)
    await asyncio.wait_for(slow[0].started.wait(), 10)
    assert not slow[0].finished
    holder[0].request_stop()
    await asyncio.wait_for(task, 10)  # shutdown cancels the blocked sweep instead of hanging on it
    assert not slow[0].finished


async def test_daemon_does_not_sweep_when_already_stopping(tmp_path, monkeypatch):
    from k3code.gateway.server import GatewayServer

    calls = []

    async def spy(self, **kw):
        calls.append(1)
        return 0

    monkeypatch.setattr(GatewayServer, "sweep_empty_sessions_async", spy)
    server = GatewayServer()
    server.request_stop()
    await daemon._sweep_empty_sessions(server)
    assert calls == []
    await server.close()
