"""The daemon-start sweep of old empty stored sessions: every `k3code agents` run, every `n` and every abandoned TUI
start leaves a 0-message row; hidden from the agent view, auto-resume and the switcher, they piled up for good."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

import pytest

import m1cmd_helpers as m1
from daemon_helpers import _stop_daemon
from k3code import daemon
from k3code.automation.engine import AutomationEngine
from k3code.gateway.sessions import SessionStore, StoredSession
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


async def _wait_until(cond, what: str, timeout: float = 10.0) -> None:
    """Poll ``cond`` until it holds: a bounded wait that fails naming ``what`` instead of stalling the suite."""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:g} s waiting for {what}")
        await asyncio.sleep(0.05)


async def test_stop_daemon_fails_fast_with_the_stack_when_shutdown_hangs():
    class _Server:
        def request_stop(self) -> None:
            pass  # a shutdown that never gets anywhere

    async def stuck_shutdown() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(stuck_shutdown())
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="(?s)did not stop within 0.2 s.*stuck_shutdown"):
        await _stop_daemon(_Server(), task, timeout=0.2)
    assert time.monotonic() - started < 5
    await asyncio.wait({task}, timeout=5)
    assert task.cancelled()


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
    try:  # the sweep runs in the background after readiness
        await asyncio.wait_for(ready.wait(), 10)
        server = holder[0]
        await _wait_until(lambda: server.store.get(old.session_id) is None, "the start sweep to delete the old row")
        assert server.store.get(old.session_id) is None
        assert server.store.get(kept.session_id) is not None
    finally:
        await _stop_daemon(holder[0] if holder else None, task)


async def test_sweep_keeps_rows_referenced_by_paused_automations(tmp_path, monkeypatch):
    now = time.time()
    server, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    server.automation = AutomationEngine(server, use_netwatch=False)
    await server.automation.start()
    ids = {n: await m1.new_session(server, tmp_path) for n in ("action", "trigger", "orphan", "orphan2")}
    added = [
        server.automation.automations.add(
            name="a",
            trigger={"type": "cron", "schedule": "daily 09:00"},
            action={"type": "prompt", "prompt": "x", "session": ids["action"]},
        ),
        server.automation.automations.add(
            name="t",
            trigger={"type": "session_event", "session": ids["trigger"], "event": "completed"},
            action={"type": "prompt", "prompt": "x"},
        ),
    ]
    # pause by id, not by name: ids are random hex, and a name that is an id prefix is still resolved by exact id,
    # then exact name, then case-insensitive name; keeping the ids here sidesteps lookup order in a sweep test
    for row in added:
        assert await server.automation.automations.pause(row["id"])
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
    try:
        await asyncio.wait_for(ready.wait(), 10)  # readiness does not wait for the sweep
        await _wait_until(lambda: slow, "the start sweep to begin")
        await asyncio.wait_for(slow[0].started.wait(), 10)
        assert not slow[0].finished
    finally:  # always stopped, also when an assertion above fails: a daemon left running can stall the loop teardown
        # shutdown cancels the blocked sweep instead of hanging on it
        await _stop_daemon(holder[0] if holder else None, task)
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


# --- tombstones: a process that still holds a swept session (a standalone stdio TUI) gets it back on its next save


def _tombstones(store: SessionStore) -> dict[str, float]:
    return dict(store._db.execute("SELECT session_id, swept_at FROM swept_sessions").fetchall())


def _row_count(store: SessionStore) -> int:
    return store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def _swept(store: SessionStore, now: float, **create) -> StoredSession:
    """An empty session the caller still holds, aged past the limit and swept away under it."""
    held = store.create(**create)
    _age(store, held.session_id, now - 31 * DAY)
    assert store.sweep_empty(now=now) == 1
    return held


class _NoTombstones:
    """The store's connection, except that writing a tombstone fails (a full disk, say)."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __enter__(self):
        return self.db.__enter__()

    def __exit__(self, *exc):
        return self.db.__exit__(*exc)

    def execute(self, sql, *args):
        if "INTO swept_sessions" in sql:
            raise sqlite3.OperationalError("database or disk is full")
        return self.db.execute(sql, *args)


def test_sweep_writes_the_tombstone_in_the_same_transaction_as_the_delete(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    now = time.time()
    a, b = store.create(), store.create()
    for sid in (a.session_id, b.session_id):
        _age(store, sid, now - 31 * DAY)

    real = store._db
    store._db = _NoTombstones(real)  # type: ignore[assignment]
    with pytest.raises(sqlite3.OperationalError):
        store.sweep_empty(now=now)
    store._db = real
    real.commit()  # whatever the failed sweep left pending must not be a delete without its tombstone
    assert store.get(a.session_id) is not None and store.get(b.session_id) is not None
    assert _tombstones(store) == {}

    assert store.sweep_empty(now=now) == 2
    assert store.get(a.session_id) is None and store.get(b.session_id) is None
    assert _tombstones(store) == {a.session_id: now, b.session_id: now}


def test_save_after_the_sweep_restores_the_row_and_clears_the_tombstone(tmp_path, caplog):
    store = SessionStore(tmp_path / "s.db")
    now = time.time()
    held = _swept(store, now, model="m1", provider="p1", cwd="/old")
    created_at = held.created_at
    assert store.get(held.session_id) is None  # reading does not bring it back: only a save does
    assert held.session_id in _tombstones(store)

    held.messages = [{"role": "user", "content": "still here"}]
    held.title = "late work"
    held.meta = {"mode": "plan", "add_dirs": ["/extra"]}
    held.cwd = "/new"
    held.usage = {"input_tokens": 3}
    held.model, held.provider = "m2", "p2"
    with caplog.at_level(logging.INFO, logger="k3code.gateway.sessions"):
        store.save(held)
        got = store.get(held.session_id)
        assert got is not None
        assert (got.messages, got.title, got.meta, got.cwd, got.usage, got.model, got.provider) == (
            [{"role": "user", "content": "still here"}],
            "late work",
            {"mode": "plan", "add_dirs": ["/extra"]},
            "/new",
            {"input_tokens": 3},
            "m2",
            "p2",
        )
        assert got.created_at == created_at and got.updated_at == held.updated_at
        assert _tombstones(store) == {}

        # the next save is an ordinary update of the restored row
        held.messages.append({"role": "assistant", "content": "ok"})
        store.save(held)
    again = store.get(held.session_id)
    assert again is not None and len(again.messages) == 2 and again.title == "late work"
    assert _row_count(store) == 1
    restored = [r for r in caplog.records if "restored" in r.getMessage() and held.session_id in r.getMessage()]
    assert len(restored) == 1 and restored[0].levelno == logging.INFO


def test_save_after_session_delete_does_not_recreate(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    now = time.time()
    plain = store.create()
    assert store.delete(plain.session_id)
    plain.messages = [{"role": "user", "content": "x"}]
    store.save(plain)
    assert store.get(plain.session_id) is None

    # swept first, then deleted by the user (the delete finds no row): the tombstone goes with the delete
    held = _swept(store, now)
    assert not store.delete(held.session_id)
    assert _tombstones(store) == {}
    held.messages = [{"role": "user", "content": "x"}]
    store.save(held)
    assert store.get(held.session_id) is None
    assert _row_count(store) == 0


def test_save_for_an_id_never_seen_stays_a_no_op(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    store.save(StoredSession(session_id="never-seen", title="t", messages=[{"role": "user", "content": "x"}]))
    assert store.get("never-seen") is None
    assert _row_count(store) == 0 and _tombstones(store) == {}


def test_sweep_prunes_tombstones_older_than_90_days(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    now = time.time()
    held = _swept(store, now)
    assert store.sweep_empty(now=now + 89 * DAY) == 0
    assert _tombstones(store) == {held.session_id: now}
    assert store.sweep_empty(now=now + 91 * DAY) == 0  # nothing to delete: the prune still runs
    assert _tombstones(store) == {}
    held.messages = [{"role": "user", "content": "too late"}]
    store.save(held)
    assert store.get(held.session_id) is None


async def test_async_sweep_prunes_and_writes_tombstones(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    now = time.time()
    old = _swept(store, now - 100 * DAY)
    fresh = store.create()
    _age(store, fresh.session_id, now - 31 * DAY)
    assert await store.sweep_empty_async(now=now) == 1
    assert _tombstones(store) == {fresh.session_id: now}
    assert old.session_id not in _tombstones(store)


def test_sweep_with_tombstones_is_idempotent(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    now = time.time()
    held = _swept(store, now)
    assert store.sweep_empty(now=now + DAY) == 0
    assert _tombstones(store) == {held.session_id: now}
    reopened = SessionStore(tmp_path / "s.db")  # the schema statements are idempotent too
    assert _tombstones(reopened) == {held.session_id: now}
    reopened.close()


def test_daemon_sweeps_while_a_stdio_process_holds_the_session(tmp_path):
    db = tmp_path / "sessions.db"
    daemon_store, stdio_store = SessionStore(db), SessionStore(db)  # two processes, two connections
    now = time.time()
    held = stdio_store.create(cwd="/proj")
    _age(daemon_store, held.session_id, now - 31 * DAY)
    assert daemon_store.sweep_empty(now=now) == 1
    assert stdio_store.get(held.session_id) is None

    held.messages = [{"role": "user", "content": "saved after the sweep"}]
    stdio_store.save(held)
    got = daemon_store.get(held.session_id)
    assert got is not None and got.messages == held.messages and got.cwd == "/proj"
    assert _tombstones(daemon_store) == {}
    assert daemon_store.sweep_empty(now=now) == 0  # it has a message now
    daemon_store.close()
    stdio_store.close()


# --- the sweep reads its candidates outside the transaction that deletes them: another process may save in the gap


def _raced_save(other: SessionStore, held: StoredSession) -> None:
    held.messages = [{"role": "user", "content": "typed while the sweep ran"}]
    other.save(held)


def _raw(sql: str):
    """One column changed behind ``save``'s back (``updated_at`` stays old), so each condition is pinned alone."""

    def change(other: SessionStore, held: StoredSession) -> None:
        other._db.execute(sql, (held.session_id,))
        other._db.commit()

    return change


@pytest.mark.parametrize(
    "change",
    [
        _raced_save,
        _raw("""UPDATE sessions SET messages = '[{"role": "user", "content": "x"}]' WHERE session_id = ?"""),
        _raw("UPDATE sessions SET title = 'named meanwhile' WHERE session_id = ?"),
        _raw("""UPDATE sessions SET meta = '{"mode": "plan"}' WHERE session_id = ?"""),
        _raw("UPDATE sessions SET updated_at = strftime('%s', 'now') WHERE session_id = ?"),
    ],
    ids=["save", "message", "title", "meta", "touched"],
)
def test_sweep_does_not_delete_a_row_saved_after_its_read(tmp_path, change):
    db = tmp_path / "sessions.db"
    daemon_store, stdio_store = SessionStore(db), SessionStore(db)  # two processes, two connections
    now = time.time()
    raced, idle = stdio_store.create(cwd="/proj"), stdio_store.create()
    for sid in (raced.session_id, idle.session_id):
        _age(daemon_store, sid, now - 31 * DAY)

    def keep(sid: str) -> bool:  # runs after the batch read, before the delete: the gap another process can hit
        if sid == raced.session_id:
            change(stdio_store, raced)
        return False

    assert daemon_store.sweep_empty(now=now, keep=keep) == 1
    assert daemon_store.get(raced.session_id) is not None
    assert daemon_store.get(idle.session_id) is None
    assert _tombstones(daemon_store) == {idle.session_id: now}
    daemon_store.close()
    stdio_store.close()
