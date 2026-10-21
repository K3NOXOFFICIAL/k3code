"""Archived sessions: a flag on the stored row that keeps it out of the default lists, and the agent view's
"archive these unused sessions?" proposals, read off the ``updated_at`` idle clock that archiving never moves."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import m1cmd_helpers as m1
from k3code import confio
from k3code.automation.engine import AutomationEngine
from k3code.config import RETENTION_DEFAULTS, retention, session_archive_days
from k3code.gateway.sessions import SessionStore

DAY = 24 * 3600.0


def _age(store: SessionStore, sid: str, updated_at: float) -> None:
    """``save`` stamps ``updated_at`` with the clock: set it behind the store's back."""
    store._db.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (updated_at, sid))
    store._db.commit()


def _worked(store: SessionStore, cwd: Path | str, updated_at: float | None = None, **meta) -> str:
    """A stored, not live, session with one user message, last saved at ``updated_at``."""
    sess = store.create(cwd=str(cwd))
    sess.messages = [{"role": "user", "content": f"work in {cwd}"}]
    sess.meta.update(meta)
    store.save(sess)
    if updated_at is not None:
        _age(store, sess.session_id, updated_at)
    return sess.session_id


def _flags(store: SessionStore, sid: str) -> tuple[float | None, float | None, float]:
    return store._db.execute(
        "SELECT archived_at, archive_declined_at, updated_at FROM sessions WHERE session_id = ?", (sid,)
    ).fetchone()


# --- the store


def test_archiving_keeps_updated_at_and_a_later_save_keeps_the_flag(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    now = time.time()
    sid = _worked(store, "/proj", now - 20 * DAY)
    assert store.set_archived([sid], now) == 1
    archived_at, _, updated_at = _flags(store, sid)
    assert archived_at == now and updated_at == now - 20 * DAY  # the idle clock did not move
    assert store.set_archived([sid], now + 5) == 0  # already archived: neither restamped nor counted

    held = store.get(sid)
    assert held is not None
    held.title = "renamed"
    store.save(held)  # save() lists its columns: the archive flag is not one of them
    assert _flags(store, sid)[0] == now
    assert store.summaries([sid])[0].archived_at == now

    store.decline_archive([sid], now)
    assert store.set_archived([sid], None) == 1
    assert _flags(store, sid)[:2] == (None, None)  # a restore also forgets the declined proposal
    assert store.set_archived([sid], None) == 0
    store.close()


def test_lists_leave_archived_sessions_out_unless_asked_for_them(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    kept, gone = _worked(store, "/proj"), _worked(store, "/proj")
    store.set_archived([gone], time.time())
    assert {s.session_id for s in store.list()} == {kept}
    assert [s.session_id for s in store.list(archived=True)] == [gone]
    assert {s.session_id for s in store.list(archived=None)} == {kept, gone}
    assert [sid for sid, _ in store.worked_in(limit=10)] == [kept]
    assert [sid for sid, _ in store.worked_in(limit=10, archived=True)] == [gone]
    assert {sid for sid, _ in store.worked_in(limit=10, archived=None)} == {kept, gone}
    assert store.most_recent().session_id == kept  # type: ignore[union-attr]
    store.close()


class _Recording:
    """The store's connection, recording each statement it runs."""

    def __init__(self, db) -> None:
        self.db, self.calls = db, []

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        self.calls.append((sql, *args))
        return self.db.execute(sql, *args)


def test_the_default_worked_in_query_still_uses_its_partial_index(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    for i in range(5):
        _worked(store, f"/proj{i}")
    real = store._db
    store._db = recording = _Recording(real)  # type: ignore[assignment]
    store.worked_in(limit=3)
    store._db = real
    ((sql, args),) = recording.calls
    plan = " ".join(str(row[-1]) for row in real.execute(f"EXPLAIN QUERY PLAN {sql}", args))
    assert "sessions_worked_in" in plan, plan
    store.close()


# --- session.archive / session.archive_decline / session.list


async def test_session_archive_skips_live_and_unknown_ids_and_restores(tmp_path, monkeypatch):
    server, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    live = await m1.new_session(server, tmp_path)
    idle = _worked(server.store, tmp_path, time.time() - 20 * DAY)
    res = (await m1.rpc(server, "session.archive", {"session_ids": [idle, live, "nope", idle]}))["result"]
    assert res == {"archived": 1, "skipped": [{"id": live, "reason": "live"}, {"id": "nope", "reason": "unknown"}]}
    assert _flags(server.store, live)[0] is None

    listed = (await m1.rpc(server, "session.list", {}))["result"]["sessions"]
    assert [r["id"] for r in listed] == [live]  # the switcher no longer shows the archived session
    assert listed[0]["archived_at"] is None
    (row,) = (await m1.rpc(server, "session.list", {"cwd": str(tmp_path), "archived": True}))["result"]["sessions"]
    archived_at, _, updated_at = _flags(server.store, idle)
    assert (row["id"], row["archived_at"], row["updated_at"]) == (idle, archived_at, updated_at)
    assert archived_at is not None
    assert (await m1.rpc(server, "session.list", {"cwd": str(tmp_path)}))["result"]["sessions"] == []

    res = (await m1.rpc(server, "session.archive", {"session_ids": [idle], "archived": False}))["result"]
    assert res == {"archived": 1, "skipped": []}
    restored = (await m1.rpc(server, "session.list", {"cwd": str(tmp_path)}))["result"]["sessions"]
    assert [r["id"] for r in restored] == [idle]
    bad = await m1.rpc(server, "session.archive", {"session_ids": idle})
    assert "error" in bad and _flags(server.store, idle)[0] is None
    declined = await m1.rpc(server, "session.archive_decline", {"session_ids": [idle, "nope"]})
    assert declined["result"] == {"declined": 1}
    await server.close()


@pytest.mark.parametrize("method", ["session.resume", "session.activate"])
async def test_attaching_to_an_archived_session_takes_it_out_of_the_archive(tmp_path, monkeypatch, method):
    server, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    sid = _worked(server.store, tmp_path, time.time() - 20 * DAY)
    server.store.decline_archive([sid], time.time())
    server.store.set_archived([sid], time.time())
    assert "result" in await m1.rpc(server, method, {"session_id": sid})
    assert _flags(server.store, sid)[:2] == (None, None)
    await server.close()


# --- session.archive_suggestions


async def _suggesting_server(tmp_path, monkeypatch, **settings):
    """A server with the automation engine running: without it nothing is proposed (as the sweep deletes nothing)."""
    server, _ = m1.make_server(tmp_path, monkeypatch, ["ok"], **settings)
    server.automation = AutomationEngine(server, use_netwatch=False)
    await server.automation.start()
    return server


async def _suggest(server, cwd) -> dict:
    return (await m1.rpc(server, "session.archive_suggestions", {"cwd": str(cwd)}))["result"]


async def test_suggestions_are_this_projects_idle_unclaimed_sessions(tmp_path, monkeypatch):
    now = time.time()
    proj, other = tmp_path / "proj", tmp_path / "other"
    proj.mkdir()
    other.mkdir()
    (tmp_path / "link").symlink_to(proj)
    server = await _suggesting_server(tmp_path, monkeypatch)
    store = server.store
    old = _worked(store, proj, now - 30 * DAY)
    spelled = _worked(store, f"{tmp_path / 'link'}/", now - 20 * DAY)  # same project, other spelling
    _worked(store, proj, now - 10 * DAY)  # younger than the 14-day default
    elsewhere = _worked(store, other, now - 30 * DAY)
    _worked(store, proj, now - 30 * DAY, origin="automation")
    empty = store.create(cwd=str(proj))
    _age(store, empty.session_id, now - 30 * DAY)
    archived = _worked(store, proj, now - 30 * DAY)
    store.set_archived([archived], now)
    looped = _worked(store, proj, now - 30 * DAY)
    server.automation.loops.create(session_id=looped, prompt="check the build", interval="daily 09:00")
    live = await m1.new_session(server, proj)
    current = await m1.new_session(server, proj)  # the client moves on: live stays live, unattached
    for sid in (live, current):
        server.live[sid].stored.messages.append({"role": "user", "content": "hi"})
        store.save(server.live[sid].stored)
        _age(store, sid, now - 30 * DAY)

    res = await _suggest(server, proj)
    assert res["days"] == 14 and res["total"] == 2
    assert [(i["id"], i["idle_days"], i["message_count"]) for i in res["items"]] == [(old, 30, 1), (spelled, 20, 1)]
    assert res["items"][0]["title"] == res["items"][0]["preview"] == f"work in {proj}"
    assert (await _suggest(server, tmp_path / "link"))["total"] == 2
    assert [i["id"] for i in (await _suggest(server, other))["items"]] == [elsewhere]

    server.live.pop(live)  # no longer live (nor current, nor attached): proposed like any other
    assert live in [i["id"] for i in (await _suggest(server, proj))["items"]]

    server.automation = None  # which rows a loop or automation points at cannot be told: propose nothing
    assert await _suggest(server, proj) == {"days": 14, "total": 0, "items": []}
    await server.close()


async def test_suggestions_threshold_comes_from_the_config_and_zero_turns_them_off(tmp_path, monkeypatch):
    now = time.time()
    server = await _suggesting_server(tmp_path, monkeypatch, retention={"session_archive_days": 0})
    _worked(server.store, tmp_path, now - 400 * DAY)
    assert await _suggest(server, tmp_path) == {"days": 0, "total": 0, "items": []}
    await server.close()

    server = await _suggesting_server(tmp_path, monkeypatch, retention={"session_archive_days": 3})
    young = _worked(server.store, tmp_path, now - 2 * DAY)
    res = await _suggest(server, tmp_path)
    assert res["days"] == 3 and res["total"] == 1 and young not in [i["id"] for i in res["items"]]
    await server.close()


async def test_config_set_of_the_archive_threshold_validates_and_applies(tmp_path, monkeypatch):
    server = await _suggesting_server(tmp_path, monkeypatch)
    sid = await m1.new_session(server, tmp_path)
    res = await m1.cmd(server, "/config set retention.session_archive_days 7", sid)
    assert "Set retention.session_archive_days" in res["output"], res
    assert (await _suggest(server, tmp_path))["days"] == 7
    await server.close()


async def test_a_declined_proposal_stays_declined_until_the_session_is_saved_again(tmp_path, monkeypatch):
    now = time.time()
    server = await _suggesting_server(tmp_path, monkeypatch)
    store = server.store
    sid = _worked(store, tmp_path, now - 40 * DAY)
    assert [i["id"] for i in (await _suggest(server, tmp_path))["items"]] == [sid]
    assert (await m1.rpc(server, "session.archive_decline", {"session_ids": [sid]}))["result"] == {"declined": 1}
    assert (await _suggest(server, tmp_path))["total"] == 0
    assert _flags(store, sid)[2] == now - 40 * DAY  # declining does not move the idle clock either

    store.decline_archive([sid], now - 30 * DAY)  # declined 30 days ago ...
    _age(store, sid, now - 35 * DAY)  # ... last saved before that: still declined
    assert (await _suggest(server, tmp_path))["total"] == 0
    _age(store, sid, now - 20 * DAY)  # used again after the decline, then left idle: asked about again
    assert [i["id"] for i in (await _suggest(server, tmp_path))["items"]] == [sid]
    await server.close()


async def test_suggestions_list_at_most_twenty_oldest_first_with_the_true_total(tmp_path, monkeypatch):
    now = time.time()
    server = await _suggesting_server(tmp_path, monkeypatch)
    ids = [_worked(server.store, tmp_path, now - (15 + n) * DAY) for n in range(25)]
    res = await _suggest(server, tmp_path)
    assert res["total"] == 25 and len(res["items"]) == 20
    assert [i["id"] for i in res["items"]] == ids[::-1][:20]
    assert [i["idle_days"] for i in res["items"]] == list(range(39, 19, -1))
    await server.close()


# --- the config knob


@pytest.mark.parametrize(
    ("raw", "days"),
    [
        (None, 14),
        ({}, 14),
        ({"session_archive_days": 7}, 7),
        ({"session_archive_days": 2.9}, 2),
        ({"session_archive_days": 0}, 0),
        ({"session_archive_days": -1}, 14),
        ({"session_archive_days": True}, 14),
        ({"session_archive_days": "7"}, 14),
    ],
)
def test_session_archive_days(raw, days):
    assert session_archive_days(SimpleNamespace(retention=raw)) == days


def test_the_archive_knob_validates_and_leaves_the_prune_knobs_alone():
    confio.validate({"retention": {"session_archive_days": 7}})
    assert retention(SimpleNamespace(retention={"session_archive_days": 7})) == RETENTION_DEFAULTS
