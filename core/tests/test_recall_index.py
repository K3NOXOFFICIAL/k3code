"""The append-only recall index in sessions.db (k3code.recall): ingest, search, backfill, and the rewrite paths."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from k3code import recall
from k3code.gateway.sessions import SessionStore, StoredSession
from k3code.session_ai import SUMMARY_PREFIX
from m1cmd_helpers import cmd, make_server, new_session
from test_auto_compaction import Recording, seed

DAY = 86400.0
GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "sessions.db")
    yield s
    s.close()


@pytest.fixture
def examined(monkeypatch):
    """How many list entries ingest looked at (extract_text calls)."""
    calls: list[int] = [0]
    real = recall.extract_text

    def counting(message):
        calls[0] += 1
        return real(message)

    monkeypatch.setattr(recall, "extract_text", counting)
    return calls


def rows(store: SessionStore, sid: str | None = None) -> list[tuple]:
    sql = "SELECT session_id, seq, role, text FROM message_archive"
    args: tuple = ()
    if sid:
        sql, args = sql + " WHERE session_id = ?", (sid,)
    return store._db.execute(sql + " ORDER BY id", args).fetchall()


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def raw_session(db: sqlite3.Connection, sid: str, messages, *, updated_at: float, cwd: str = "/p") -> None:
    """A row as an older k3code wrote it: no archive, no archive_state."""
    body = messages if isinstance(messages, str) else json.dumps(messages)
    db.execute(
        "INSERT INTO sessions (session_id, cwd, messages, created_at, updated_at) VALUES (?,?,?,?,?)",
        (sid, cwd, body, updated_at, updated_at),
    )
    db.commit()


# ── schema ────────────────────────────────────────────────────────────


def test_schema_is_idempotent_and_upgrades_a_pre_index_database(tmp_path):
    path = tmp_path / "sessions.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', model TEXT NOT NULL"
        " DEFAULT '', provider TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', messages TEXT NOT NULL DEFAULT"
        " '[]', usage TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL"
        " DEFAULT 0)"
    )
    old.execute("INSERT INTO sessions (session_id, messages) VALUES ('old1', ?)", (json.dumps([user("hello")]),))
    old.commit()
    old.close()
    for _ in range(2):
        s = SessionStore(path)
        assert s.fts is True
        assert recall.ensure_schema(s._db) is True
        names = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master")}
        assert {"message_archive", "archive_state", "message_fts"} <= names
        assert recall.backfill_pending(s._db) == 1  # nothing read in __init__
        assert rows(s) == []
        s.close()


# ── ingest ────────────────────────────────────────────────────────────


def test_saving_one_message_at_a_time_archives_only_the_delta(store, examined):
    """Performance guard: 400 saves of a growing list each read and insert O(1) entries, at their list position."""
    sess = store.create(cwd="/p")
    for i in range(400):
        sess.messages.append(user(f"message number {i}") if i % 2 == 0 else assistant(f"reply number {i}"))
        before_rows, before_calls = len(rows(store)), examined[0]
        store.save(sess)
        assert examined[0] - before_calls == 1, f"save {i} read {examined[0] - before_calls} entries"
        assert len(rows(store)) - before_rows == 1
    assert [r[1] for r in rows(store)] == list(range(400))


def test_a_rewritten_list_keeps_the_old_rows_and_adds_the_summary(store, examined):
    sess = store.create(cwd="/p")
    sess.messages = [user(f"early topic {i} walrus") if i % 2 == 0 else assistant(f"answer {i}") for i in range(10)]
    store.save(sess)
    sess.messages = [user(SUMMARY_PREFIX + "they discussed walruses"), *sess.messages[-2:]]
    examined[0] = 0
    store.save(sess)
    assert examined[0] == 3  # the whole new list was offered again
    got = rows(store, sess.session_id)
    assert len(got) == 11  # 10 old rows kept, one summary added, the two kept messages not duplicated
    summary = [r for r in got if r[2] == "summary"]
    assert summary == [(sess.session_id, 10, "summary", "they discussed walruses")]  # positions after every old row
    (hit,) = recall.search(store._db, "walruses")
    assert hit.kind == "summary" and hit.role == "user"


def test_identical_texts_are_archived_once(store):
    sess = store.create()
    msgs = [user("yes"), assistant("done"), user("yes")]
    with store._db:
        assert recall.ingest(store._db, sess.session_id, "", msgs, time.time()) == 2
    with store._db:
        store._db.execute("DELETE FROM archive_state")
        assert recall.ingest(store._db, sess.session_id, "", msgs, time.time()) == 0


def test_secrets_are_scrubbed_before_they_are_stored(store):
    sess = store.create()
    sess.messages = [user(f"my token is {GHP} please keep it")]
    store.save(sess)
    (row,) = rows(store)
    assert GHP not in row[3] and "please keep it" in row[3]
    assert GHP not in "".join(store._db.execute("SELECT text FROM message_archive").fetchone())


def test_tool_results_and_tool_calls_are_not_indexed(store):
    sess = store.create()
    call = {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "toolsecretword"}'}}
    sess.messages = [
        {"role": "system", "content": "system words"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": "toolsecretword output"},
        {"role": "assistant", "content": [{"type": "text", "text": "part one "}, {"type": "text", "text": "two"}]},
    ]
    store.save(sess)
    assert [r[2:] for r in rows(store)] == [("assistant", "part one two")]
    assert recall.search(store._db, "toolsecretword") == []


def test_checkpoint_indexes_and_a_deleted_row_gets_no_archive(store):
    sess = store.create(cwd="/p")
    store.checkpoint(sess.session_id, [user("checkpointed capybara")])
    assert [r[3] for r in rows(store)] == ["checkpointed capybara"]
    store.delete(sess.session_id)
    store.checkpoint(sess.session_id, [user("ghost text")])
    sess.messages = [user("ghost text")]
    store.save(sess)
    assert rows(store) == []


def test_delete_forgets_the_archive_and_its_fts_rows(store):
    keep, gone = store.create(), store.create()
    for s in (keep, gone):
        s.messages = [user(f"shared okapi words {s.session_id}")]
        store.save(s)
    assert store.delete(gone.session_id)
    assert {r[0] for r in rows(store)} == {keep.session_id}
    assert store._db.execute("SELECT session_id FROM archive_state").fetchall() == [(keep.session_id,)]
    fts = store._db.execute("SELECT rowid FROM message_fts WHERE message_fts MATCH 'okapi'").fetchall()
    assert len(fts) == 1
    assert [h.session_id for h in recall.search(store._db, "okapi")] == [keep.session_id]


# ── search ────────────────────────────────────────────────────────────


@pytest.fixture
def corpus(store):
    now = time.time()
    ids = {}
    for i in range(300):
        sid = f"s{i:03d}"
        raw_session(store._db, sid, [], updated_at=now - i, cwd=f"/work/p{i % 7}")
        with store._db:
            recall.ingest(
                store._db, sid, f"/work/p{i % 7}", [user(f"routine work item {i}"), assistant("fixed it")], now - i
            )
    old = "oldone"
    raw_session(store._db, old, [], updated_at=now - 120 * DAY, cwd="/work/legacy")
    with store._db:
        recall.ingest(store._db, old, "/work/legacy", [user("we chose the kumquat migration plan")], now - 120 * DAY)
    for sid, cwd in (("pa", "/proj/a"), ("pb", "/proj/b")):
        raw_session(store._db, sid, [], updated_at=now - 5 * DAY, cwd=cwd)
        with store._db:
            recall.ingest(store._db, sid, cwd, [user("rotate the zeppelin deploy keys")], now - 5 * DAY)
    multi = "multi"
    raw_session(store._db, multi, [], updated_at=now, cwd="/work/m")
    with store._db:
        recall.ingest(
            store._db, multi, "/work/m", [user(f"marmot note {i}") for i in range(5)] + [user("foo bar baz")], now
        )
    ids["old"], ids["multi"] = old, multi
    return ids


def test_a_four_month_old_session_is_found_among_hundreds(store, corpus):
    (hit,) = recall.search(store._db, "Kumquat migration?")
    assert hit.session_id == "oldone" and hit.id == "oldone:0" and hit.cwd == "/work/legacy"
    assert hit.at < time.time() - 100 * DAY and "[kumquat]" in hit.snippet and hit.matched_terms == 2


def test_the_current_project_ranks_first_and_project_filters(store, corpus):
    assert recall.search(store._db, "zeppelin", current_project="/proj/a")[0].session_id == "pa"
    assert recall.search(store._db, "zeppelin", current_project="/proj/b")[0].session_id == "pb"
    assert [h.session_id for h in recall.search(store._db, "zeppelin", project="/proj/b")] == ["pb"]
    assert recall.search(store._db, "zeppelin", project="/pro") == []  # a name prefix is not a parent directory
    assert {h.session_id for h in recall.search(store._db, "zeppelin", project="/proj/")} == {"pa", "pb"}


def test_since_session_limit_and_per_session(store, corpus):
    assert recall.search(store._db, "kumquat", since=time.time() - 30 * DAY) == []
    assert [h.session_id for h in recall.search(store._db, "zeppelin", session_id="pa")] == ["pa"]
    assert len(recall.search(store._db, "routine", limit=5)) == 5
    assert len(recall.search(store._db, "marmot")) == 2  # per_session default
    assert len(recall.search(store._db, "marmot", per_session=4, limit=10)) == 4


def test_hostile_queries_never_reach_match_raw(store, corpus):
    hits = recall.search(store._db, '-foo "bar NEAR/3 baz:*')
    assert hits and hits[0].session_id == "multi" and hits[0].matched_terms == 3
    assert recall.search(store._db, "") == []
    assert recall.search(store._db, "the and of und der") == []
    assert recall.search(store._db, 'AND OR NOT * ^ ( ) " :') == []


def test_the_substring_fallback_finds_the_same_top_hit(store, corpus):
    for query in ("kumquat migration", "zeppelin"):
        fts = recall.search(store._db, query, current_project="/proj/b")
        like = recall.search(store._db, query, current_project="/proj/b", fts=False)
        assert like[0].session_id == fts[0].session_id
    assert recall.search(store._db, "kumquat", fts=False)[0].snippet.startswith("we chose the kumquat")


def test_read_around_reads_the_archive_by_position(store):
    sess = store.create()
    sess.messages = [user(f"line {i}") if i % 3 else {"role": "tool", "content": "x"} for i in range(12)]
    store.save(sess)
    got = recall.read_around(store._db, sess.session_id, 5, before=1, after=2)
    assert [m.text for m in got] == ["line 4", "line 5", "line 7", "line 8"]


# ── backfill ──────────────────────────────────────────────────────────


def test_backfill_indexes_old_sessions_newest_first_and_skips_malformed_rows(store, examined):
    now = time.time()
    for i in range(5):
        raw_session(store._db, f"b{i}", [user(f"backfilled wombat {i}"), assistant(f"ok {i}")], updated_at=now - i)
    raw_session(store._db, "bad", '[{"role": "user", "content": "broken', updated_at=now + 1)
    raw_session(store._db, "empty", [], updated_at=now + 2)
    assert recall.backfill_pending(store._db) == 6
    assert store.backfill_archive_batch(max_sessions=3) == 3
    order = [r[0] for r in store._db.execute("SELECT session_id FROM archive_state ORDER BY rowid")]
    assert order == ["bad", "b0", "b1"]
    assert store._db.execute("SELECT indexed FROM archive_state WHERE session_id='bad'").fetchone() == (-1,)
    assert store.backfill_archive_batch(max_sessions=8, max_bytes=1) == 1  # the byte budget stops the batch
    assert store.backfill_archive_batch() == 2
    assert store.backfill_archive_batch() == 0  # the malformed row is not retried
    count = len(rows(store))
    assert count == 10 and store.backfill_archive_batch() == 0 and len(rows(store)) == count
    assert recall.archive_count(store._db) == 5 and recall.backfill_pending(store._db) == 0
    hit = recall.search(store._db, "wombat", session_id="b3")[0]
    assert hit.session_id == "b3" and hit.at == pytest.approx(now - 3)
    # a save after the backfill takes the delta path: the SQL extraction and the Python one agree
    sess = store.get("b0")
    sess.messages.append(user("appended later"))
    examined[0] = 0
    store.save(sess)
    assert examined[0] == 1
    assert [r[1:] for r in rows(store, "b0")] == [(0, "user", "backfilled wombat 0"), (1, "assistant", "ok 0"),
                                                   (2, "user", "appended later")]  # fmt: skip


def test_backfill_and_live_ingest_agree_on_parts_summaries_and_secrets(tmp_path, examined):
    msgs = [
        user(SUMMARY_PREFIX + f"summary with {GHP}"),
        {"role": "assistant", "content": [{"text": "a"}, {"type": "image"}, {"text": 5}, {"text": "b"}]},
        {"role": "tool", "content": "skip"},
        "a stray string entry",
        user("  "),
    ]
    live, back = SessionStore(tmp_path / "live.db"), SessionStore(tmp_path / "back.db")
    s = live.create()
    s.messages = msgs
    live.save(s)
    raw_session(back._db, s.session_id, msgs, updated_at=time.time())
    assert back.backfill_archive_batch() == 1
    assert rows(live) == rows(back)
    (_, _, kind, text), second = rows(back)
    assert kind == "summary" and text.startswith("summary with ") and GHP not in text
    assert second[2:] == ("assistant", "ab")
    back_state = back._db.execute("SELECT indexed, tail FROM archive_state").fetchone()
    assert back_state == live._db.execute("SELECT indexed, tail FROM archive_state").fetchone()
    live.close()
    back.close()


# ── rewrite paths and the gateway loop ────────────────────────────────


def _forget_archive(store: SessionStore) -> None:
    with store._db:
        store._db.execute("DELETE FROM message_archive")
        store._db.execute("DELETE FROM archive_state")


async def test_compaction_archives_the_folded_messages_first(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={"keep_messages": 6})
    _forget_archive(server.store)  # saved by an older version: the backfill has not reached it
    live.stored.messages[5] = user("the platypus decision was made here")  # and an edit never saved
    assert await server.compact_session(live) > 0
    assert not any(m.get("content") == "the platypus decision was made here" for m in live.stored.messages)
    hits = recall.search(server.store._db, "platypus decision")
    assert any(h.session_id == live.session_id and h.kind == "user" for h in hits)
    assert any(r[2] == "summary" for r in rows(server.store, live.session_id))
    await server.close()


async def test_clear_archives_the_saved_and_the_unsaved_transcript(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    live.stored.messages = [user("saved quokka text")]
    server.store.save(live.stored)
    _forget_archive(server.store)
    live.stored.messages.append(assistant("unsaved numbat tail"))
    await cmd(server, "/clear", sid)
    assert server.store.get(sid).messages == []
    for word in ("quokka", "numbat"):
        assert [h.session_id for h in recall.search(server.store._db, word)] == [sid]
    await server.close()


async def test_serve_runs_the_backfill_until_a_batch_finds_nothing(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "ARCHIVE_BACKFILL_PAUSE_S", 0)
    results = iter([3, 1, 0, 99])
    calls: list[str] = []

    async def on_writer(name, *args):
        calls.append(name)
        return next(results)

    monkeypatch.setattr(server.store, "_on_writer", on_writer)
    seen: dict[str, bool] = {}

    async def serve_stdio():
        for _ in range(200):
            if server._archive_backfill is not None and server._archive_backfill.done():
                break
            await asyncio.sleep(0.01)
        seen["done"] = server._archive_backfill.done()

    monkeypatch.setattr(server, "_serve_stdio", serve_stdio)
    await server.serve()
    assert seen == {"done": True} and calls == ["backfill_archive_batch"] * 3
    await server.close()


async def test_a_real_backfill_through_the_writer_thread(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    for i in range(20):
        raw_session(server.store._db, f"r{i}", [user(f"pangolin {i}")], updated_at=time.time() - i)
    await server._archive_backfill_loop()
    assert recall.backfill_pending(server.store._db) == 0
    assert len(recall.search(server.store._db, "pangolin", limit=20, per_session=1)) == 20
    await server.close()


def test_stored_session_import_is_indexed(store):
    sess = StoredSession(session_id=store.new_id(), cwd="/imp", messages=[user("imported axolotl")])
    store.insert(sess)
    assert [h.cwd for h in recall.search(store._db, "axolotl")] == ["/imp"]
