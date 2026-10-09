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
        _age(first.store, sid, now - (6 if name == "young_empty" else 8) * DAY)
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
    _age(server.store, live_sid, now - 8 * DAY)
    _age(server.store, current, now - 8 * DAY)

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
    assert store.sweep_empty(now=now) == 0  # younger than the 7-day default
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
        _age(store, sid, time.time() - 30 * DAY)
    store.close()
    ready, holder = asyncio.Event(), []
    task = asyncio.create_task(
        daemon.run_daemon(home=home, install_signals=False, ready_event=ready, server_out=holder, watchdog_interval=5)
    )
    await asyncio.wait_for(ready.wait(), 10)
    server = holder[0]
    try:
        assert server.store.get(old.session_id) is None
        assert server.store.get(kept.session_id) is not None
    finally:
        server.request_stop()
        await asyncio.wait_for(task, 10)
