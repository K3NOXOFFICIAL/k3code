"""The seams between a live session and its stored row.

* what a session has read (the write/edit guard's stamps) survives a close, a resume, a fork and a daemon restart;
* an end-of-turn snapshot queued on the writer thread cannot put older meta or title back over a newer save;
* a meta-only change (goal state, mode, title) no longer rewrites the transcript on the event loop.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

from k3code.commands.fork import fork_session
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.permissions.engine import PermissionMode
from k3code.providers.types import Message
from k3code.tools import FileStamps
from m1cmd_helpers import make_server, new_session, rpc
from test_autonomy_gateway import call, make, run_turn

AUTONOMY = {"plan_first": False, "proposals": False}

READ = [
    {
        "type": "tool_call",
        "id": "r1",
        "match": "READ-IT",
        "tool_results": 0,  # only the first call: the script is shared with the later turn, whose history says READ-IT
        "name": "read",
        "arguments": {"path": "f.txt"},
    },
    {"type": "text", "match": "READ-IT", "tool_results": 1, "text": "read it"},
]
EDIT = [
    {
        "type": "tool_call",
        "id": "e1",
        "match": "EDIT-IT",
        "when": "turn_first",
        "name": "edit",
        "arguments": {"path": "f.txt", "old_string": "alpha", "new_string": "beta"},
    },
    {"type": "text", "match": "EDIT-IT", "when": "turn_after_tool", "text": "edited"},
]


_REAL_SAVE = SessionStore.save  # the writer-held test patches the class attribute


def _tool_results(server) -> list[int]:
    return [str(m["content"]) for m in server.session.stored.messages if m.get("role") == "tool"]


def _project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "f.txt").write_text("alpha\n")
    return proj


def _raw_messages(store: SessionStore, session_id: str) -> str:
    return store._db.execute("SELECT messages FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]


# ── FileStamps export / load ──────────────────────────────────────────


def test_stamps_survive_a_json_round_trip_and_a_changed_file_is_still_refused(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha\n")
    seen = FileStamps()
    seen.record(f)
    revived = FileStamps()
    revived.load(json.loads(json.dumps(seen.export())))
    assert revived.refusal("edit", f) is None
    f.write_text("alpha and what the user added\n")  # the mtime and size are still compared
    assert "changed since you read it" in (revived.refusal("edit", f) or "")
    assert "have not read" in (FileStamps().refusal("edit", f) or "")


def test_export_keeps_only_the_newest_stamps_and_load_skips_garbage(tmp_path):
    seen = FileStamps()
    files = []
    for i in range(FileStamps.KEEP + 10):
        f = tmp_path / f"f{i}.txt"
        f.write_text("x")
        seen.record(f)
        files.append(f)
    out = seen.export()
    assert len(out) == FileStamps.KEEP
    assert str(files[-1]) in out and str(files[0]) not in out
    junk = FileStamps()
    for bad in (
        None,
        [],
        "x",
        3,
        {"p": [1]},
        {"p": ["a", 2]},
        {"p": [True, 2]},
        {"p": [1.5, 2]},
        {"p": 7},
        {1: [1, 2]},
    ):
        junk.load(bad)  # a damaged meta must not stop a session from opening
    assert junk.export() == {}
    junk.load({"p": "no", "good": [5, 6]})
    assert junk.export() == {"good": [5, 6]}


# ── FIX 1: a resumed session knows what it has read ───────────────────


async def test_edit_after_close_and_resume_in_one_daemon_is_not_refused(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    server = make(tmp_path, monkeypatch, [*READ, *EDIT], mode="auto", autonomy=AUTONOMY)
    await call(server, "session.create", {"cwd": str(proj)})
    sid = server.session.session_id
    await run_turn(server, "READ-IT please")
    assert server.store.get(sid).meta["file_stamps"]  # written with the turn
    await call(server, "session.create", {"cwd": str(proj)})  # the user starts another session ...
    assert (await call(server, "session.close", {"session_id": sid}))["closed"]
    await call(server, "session.resume", {"session_id": sid})  # ... and comes back to the first
    await run_turn(server, "EDIT-IT please")
    assert _tool_results(server) == ["1\talpha", "Edited: 1 replacement"]  # no second read, no refusal
    assert (proj / "f.txt").read_text() == "beta\n"


async def test_edit_after_a_daemon_restart_is_not_refused(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    first = make(tmp_path, monkeypatch, READ, mode="auto", autonomy=AUTONOMY)
    await call(first, "session.create", {"cwd": str(proj)})
    sid = first.session.session_id
    await run_turn(first, "READ-IT please")
    await first.close()
    second = make(tmp_path, monkeypatch, EDIT, mode="auto", autonomy=AUTONOMY)  # same sessions.db, a new process
    await call(second, "session.resume", {"session_id": sid})
    await run_turn(second, "EDIT-IT please")
    assert _tool_results(second) == ["1\talpha", "Edited: 1 replacement"]
    assert (proj / "f.txt").read_text() == "beta\n"


async def test_a_file_changed_while_the_session_was_closed_is_still_refused(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    server = make(tmp_path, monkeypatch, [*READ, *EDIT], mode="auto", autonomy=AUTONOMY)
    await call(server, "session.create", {"cwd": str(proj)})
    sid = server.session.session_id
    await run_turn(server, "READ-IT please")
    await call(server, "session.create", {"cwd": str(proj)})
    await call(server, "session.close", {"session_id": sid})
    (proj / "f.txt").write_text("alpha\nthe user's own line\n")  # saved in an editor meanwhile
    await call(server, "session.resume", {"session_id": sid})
    await run_turn(server, "EDIT-IT please")
    assert len(_tool_results(server)) == 2 and "changed since you read it" in _tool_results(server)[-1]
    assert (proj / "f.txt").read_text() == "alpha\nthe user's own line\n"


async def test_a_fork_starts_with_the_stamps_of_the_session_it_copies(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    server = make(tmp_path, monkeypatch, READ, mode="auto", autonomy=AUTONOMY)
    await call(server, "session.create", {"cwd": str(proj)})
    live = server.session
    await run_turn(server, "READ-IT please")
    forked = fork_session(server.store, server.store.get(live.session_id), "copy")
    assert server.live_for(forked).file_stamps.refusal("edit", proj / "f.txt") is None
    fresh = server.store.create(cwd=str(proj))
    assert "have not read" in (server.live_for(fresh).file_stamps.refusal("edit", proj / "f.txt") or "")


async def test_a_fork_does_not_inherit_the_goal_or_the_background_standing(tmp_path, monkeypatch):
    """A copy of an ACTIVE goal is a second driver of the same objective (the watchdog kicks every stored session whose
    goal is active); a copy of a background / automation session is counted against the background cap and taken for
    the automation's own session. The conversation, the mode and the stamps are the point of a fork; those stay."""
    proj = _project(tmp_path)
    server = make(tmp_path, monkeypatch, READ, mode="auto", autonomy=AUTONOMY)
    await call(server, "session.create", {"cwd": str(proj)})
    src = server.store.get(server.session.session_id)
    src.meta.update(
        {
            "goal": {"status": "active", "text": "ship it"},
            "background": True,
            "origin": "automation",
            "origin_session": "x",
        }
    )
    src.meta["mode"] = "auto"
    forked = fork_session(server.store, src, "copy")
    assert forked.meta["forked_from"] == src.session_id and forked.meta["mode"] == "auto"
    assert not {"goal", "background", "origin", "origin_session"} & set(forked.meta)
    assert src.meta["goal"]["status"] == "active"  # the source keeps its own
    assert server.store.with_goal_status("active") == [] or all(
        s.session_id != forked.session_id for s in server.store.with_goal_status("active")
    )


# ── FIX 2: a stale end-of-turn snapshot cannot revert newer meta or title ─


def _persisting_server(tmp_path):
    srv = GatewayServer.__new__(GatewayServer)
    srv.store = SessionStore(tmp_path / "s.db")
    srv._chain_key = "m"
    srv.config = SimpleNamespace(default_model="m")
    stored = srv.store.create(title="t", model="m", cwd="/p")
    loop = SimpleNamespace(turn_messages=[Message(role="user", content="one"), Message(role="assistant", content="x")])
    live = SimpleNamespace(
        session_id=stored.session_id,
        stored=stored,
        loop=loop,
        last_checkpoint=0.0,
        checkpoint_task=None,
        persist_warned=False,
    )
    return srv, stored, live


async def _persist_while_writer_is_held(srv, stored, live, command, *, cancel: bool = False) -> list[int]:
    """Run ``_persist_turn`` with the writer thread held on purpose, run ``command`` (typed meanwhile) on the loop
    thread, then let the writer go (``cancel``: after cancelling the persist, as a second /stop or a shutdown does).
    Returns the sizes of the transcripts the writer thread saved."""
    entered, gate, done, full_saves = threading.Event(), threading.Event(), threading.Event(), []
    real = SessionStore.save

    def held_save(self, s):  # the writer: a big transcript, or queued behind a checkpoint
        entered.set()
        gate.wait(10)
        full_saves.append(len(s.messages))
        real(self, s)
        done.set()

    SessionStore.save = held_save  # type: ignore[method-assign]
    try:
        task = asyncio.create_task(srv._persist_turn(live, live.loop))
        while not entered.is_set():
            await asyncio.sleep(0.005)  # the snapshot (taken before the command) is on the writer thread
        command()
        if cancel:
            task.cancel()
        gate.set()
        try:
            await task
        except asyncio.CancelledError:
            if not cancel:
                raise
        if cancel:  # the persist is gone, its shielded write is not: wait for it and for the repair after it
            while not done.is_set():
                await asyncio.sleep(0.005)
            deadline = time.monotonic() + 3
            while srv.store.get(stored.session_id).meta != stored.meta and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
    finally:
        gate.set()
        SessionStore.save = real  # type: ignore[method-assign]
    return full_saves


async def test_a_command_saved_while_the_snapshot_waited_is_not_reverted(tmp_path):
    srv, stored, live = _persisting_server(tmp_path)

    def command() -> None:  # /autocompact --session off and a rename, saved on the loop thread as the commands do
        stored.meta["autocompact"] = {"enabled": False}
        stored.title = "renamed"
        _REAL_SAVE(srv.store, stored)

    full_saves = await _persist_while_writer_is_held(srv, stored, live, command)
    row = srv.store.get(stored.session_id)
    assert row.meta.get("autocompact") == {"enabled": False} and row.title == "renamed"
    assert len(row.messages) == 2  # the end-of-turn transcript is what the snapshot wrote
    assert full_saves == [2]  # ... and it was written once: the repair is a meta-only write
    srv.store.close()


async def test_a_cancelled_persist_still_puts_the_newer_meta_back(tmp_path):
    srv, stored, live = _persisting_server(tmp_path)

    def command() -> None:
        stored.meta["autocompact"] = {"enabled": False}
        _REAL_SAVE(srv.store, stored)

    await _persist_while_writer_is_held(srv, stored, live, command, cancel=True)
    row = srv.store.get(stored.session_id)
    assert row.meta.get("autocompact") == {"enabled": False} and len(row.messages) == 2
    srv.store.close()


async def test_nothing_is_rewritten_when_no_command_ran_meanwhile(tmp_path):
    srv, stored, live = _persisting_server(tmp_path)
    calls: list[str] = []
    real = SessionStore.save_meta

    def spy(self, *a, **kw):
        calls.append("save_meta")
        return real(self, *a, **kw)

    SessionStore.save_meta = spy  # type: ignore[method-assign]
    try:
        await _persist_while_writer_is_held(srv, stored, live, lambda: None)
    finally:
        SessionStore.save_meta = real  # type: ignore[method-assign]
    assert calls == []
    assert len(srv.store.get(stored.session_id).messages) == 2
    srv.store.close()


# ── FIX 3: a meta-only change does not rewrite the transcript ─────────


def test_save_meta_leaves_the_transcript_alone_and_the_goal_index_follows(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    sess = store.create(title="t", cwd="/p")
    sess.messages = [{"role": "user", "content": "build it"}, {"role": "assistant", "content": "ok"}]
    store.save(sess)
    before = _raw_messages(store, sess.session_id)
    sess.messages = [*sess.messages, {"role": "user", "content": "in memory only, not saved"}]
    sess.meta["goal"] = {"status": "active", "goal": "ship"}
    sess.title = "named"
    store.save_meta_of(sess)
    assert _raw_messages(store, sess.session_id) == before
    row = store.get(sess.session_id)
    assert row.title == "named" and row.meta["goal"]["status"] == "active" and len(row.messages) == 2
    assert [s.message_count for s in store.list()] == [2]
    assert [s.session_id for s in store.with_goal_status("active")] == [sess.session_id]
    sess.meta["goal"]["status"] = "paused"
    store.save_meta_of(sess)
    assert store.with_goal_status("active") == []
    assert [s.session_id for s in store.with_goal_status("paused")] == [sess.session_id]
    plan = store._db.execute(
        "EXPLAIN QUERY PLAN SELECT session_id FROM sessions WHERE json_extract(meta, '$.goal.status') IN (?)",
        ("active",),
    ).fetchall()
    assert any("sessions_goal_status" in str(r) for r in plan)  # the index is still the one the update maintains
    assert store.save_meta(sess.session_id, {"origin": "automation"}) is True
    assert store.worked_in(limit=5) == []  # worked_in reads the same meta column
    store.close()


def test_save_meta_of_a_swept_row_restores_it_and_a_deleted_row_stays_gone(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    swept = store.create(cwd="/p")
    assert store.sweep_empty(now=time.time() + 40 * 24 * 3600) == 1
    assert store.get(swept.session_id) is None
    swept.meta["mode"] = "plan"
    store.save_meta_of(swept)  # another process held the session open past the sweep: same as save()
    assert store.get(swept.session_id).meta == {"mode": "plan"}
    store.delete(swept.session_id)
    assert store.save_meta(swept.session_id, {"mode": "auto"}, "x") is False
    store.save_meta_of(swept)
    assert store.get(swept.session_id) is None
    store.close()


def test_a_title_with_a_lone_surrogate_does_not_stop_a_meta_save(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    sess = store.create(title="t")
    sess.title = "bad \ud800 title"
    store.save_meta_of(sess)
    assert store.get(sess.session_id).title.startswith("bad ")
    store.close()


def _spy_full_saves(monkeypatch) -> list[int]:
    saved: list[int] = []
    real = SessionStore.save

    def spy(self, s):
        saved.append(len(s.messages))
        real(self, s)

    monkeypatch.setattr(SessionStore, "save", spy)
    return saved


async def test_a_goal_state_change_does_not_rewrite_the_transcript(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    live.stored.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 1000} for i in range(40)]
    server.store.save(live.stored)
    before = _raw_messages(server.store, sid)
    live.stored.messages = [*live.stored.messages, {"role": "user", "content": "not saved yet"}]
    saved = _spy_full_saves(monkeypatch)
    mgr = server.goal_manager(live)
    mgr.set("ship it")
    assert [s.session_id for s in server.store.with_goal_status("active")] == [sid]
    mgr.pause("user-paused")
    assert [s.session_id for s in server.store.with_goal_status("paused")] == [sid]
    assert server.store.with_goal_status("active") == []
    mgr.resume()
    mgr.clear()
    assert server.store.get(sid).meta.get("goal") is None
    assert saved == []  # no full-row save at all
    assert _raw_messages(server.store, sid) == before
    await server.close()


async def test_switching_the_mode_and_renaming_do_not_rewrite_the_transcript(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    live.stored.messages = [{"role": "user", "content": "hello"}]
    server.store.save(live.stored)
    before = _raw_messages(server.store, sid)
    saved = _spy_full_saves(monkeypatch)
    live.set_mode(PermissionMode.PLAN)
    assert (await rpc(server, "session.title", {"session_id": sid, "title": "my title"}))["result"]["ok"]
    assert saved == []
    row = server.store.get(sid)
    assert row.meta["mode"] == "plan" and row.title == "my title"
    assert _raw_messages(server.store, sid) == before
    await server.close()


def test_save_meta_never_inserts_a_missing_row(tmp_path):
    """save_meta must never insert: an UPDATE of a missing row stays missing."""
    store = SessionStore(tmp_path / "s.db")
    assert store.save_meta("nope", {"a": 1}, "t") is False
    with sqlite3.connect(tmp_path / "s.db") as raw:
        assert raw.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    store.close()
