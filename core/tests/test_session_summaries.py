"""A session listing reads metadata columns, never the transcript: one 100 MB row must not slow every `session.list`."""

from __future__ import annotations

import json
import time

import pytest

from k3code.gateway.sessions import PREVIEW_CHARS, SessionStore, summarize_messages


def _session(store: SessionStore, messages: list[dict], **kw) -> str:
    sess = store.create(title=kw.pop("title", ""), cwd=kw.pop("cwd", "/p"))
    sess.messages = messages
    store.save(sess)
    return sess.session_id


def test_summarize_picks_the_first_non_empty_user_message():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": ""},
        {"role": "user", "content": "x" * 500},
        {"role": "user", "content": "later"},
    ]
    count, preview = summarize_messages(msgs)
    assert count == 4 and preview == "x" * PREVIEW_CHARS
    assert summarize_messages([]) == (0, "")
    assert summarize_messages([{"role": "assistant", "content": "hi"}]) == (1, "")


def test_listing_carries_count_and_preview_without_the_transcript(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    sid = _session(store, [{"role": "user", "content": "hello there"}, {"role": "assistant", "content": "hi"}])
    [row] = store.list()
    assert (row.session_id, row.message_count, row.preview) == (sid, 2, "hello there")
    assert not hasattr(row, "messages")


def test_a_new_session_lists_as_empty(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    store.create(cwd="/p")
    [row] = store.list()
    assert row.message_count == 0 and row.preview == ""


def test_listing_never_parses_a_transcript(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "s.db")
    for i in range(5):
        _session(store, [{"role": "user", "content": f"q{i}" + "x" * 50_000}])
    calls = []
    real = json.loads
    monkeypatch.setattr(
        "k3code.gateway.sessions.json.loads", lambda s, *a, **k: (calls.append(len(s)), real(s, *a, **k))[1]
    )
    assert len(store.list(limit=50)) == 5
    assert calls == []


@pytest.mark.serial  # wall-clock limit; load-sensitive under the 8-worker run, passes alone
def test_a_huge_row_does_not_slow_the_listing(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    big = json.dumps([{"role": "user", "content": "first"}] + [{"role": "tool", "content": "z" * 1_000_000}] * 100)
    store._db.execute(
        "INSERT INTO sessions (session_id, cwd, messages, updated_at, message_count, preview)"
        " VALUES ('big','/p',?,1,101,'first')",
        (big,),
    )
    store._db.commit()
    began = time.perf_counter()
    [row] = store.list(limit=50)
    assert time.perf_counter() - began < 0.05
    assert (row.message_count, row.preview) == (101, "first")


def test_rows_from_before_the_columns_are_filled_in_once(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    blob = json.dumps([{"role": "user", "content": "old question"}, {"role": "assistant", "content": "a"}])
    store._db.execute("INSERT INTO sessions (session_id, cwd, messages, updated_at) VALUES ('old','/p',?,1)", (blob,))
    store._db.execute("UPDATE sessions SET message_count = -1, preview = '' WHERE session_id = 'old'")
    store._db.commit()
    [first] = store.list()
    assert (first.message_count, first.preview) == (2, "old question")
    stored = store._db.execute("SELECT message_count, preview FROM sessions WHERE session_id='old'").fetchone()
    assert tuple(stored) == (2, "old question")  # written back: the next listing reads columns only


def test_summaries_keep_the_requested_order_and_skip_unknown_ids(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    a = _session(store, [{"role": "user", "content": "a"}])
    b = _session(store, [{"role": "user", "content": "b"}])
    got = store.summaries([b, "missing", a])
    assert [s.session_id for s in got] == [b, a]


def test_an_old_database_gains_the_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT, model TEXT, provider TEXT, cwd TEXT,"
        " messages TEXT, usage TEXT, created_at REAL, updated_at REAL)"
    )
    db.execute(
        "INSERT INTO sessions VALUES ('s1','t','m','p','/p',?, '{}', 1, 2)",
        (json.dumps([{"role": "user", "content": "carried over"}]),),
    )
    db.commit()
    db.close()
    [row] = SessionStore(path).list()
    assert (row.message_count, row.preview) == (1, "carried over")


def test_worked_in_decides_by_the_count_column_and_falls_back_to_the_transcript_only_for_old_rows(tmp_path):
    """The agent view's `session.list {cwd}` scans the newest 2000 rows through `worked_in`; the row filter must not
    touch the transcript column, or sqlite reads every stored transcript off disk for each listing."""
    store = SessionStore(tmp_path / "s.db")
    used = _session(store, [{"role": "user", "content": "hello"}])
    empty = _session(store, [])
    db = store._db
    # Written before the column existed: only this row may be decided by its transcript.
    db.execute(
        "INSERT INTO sessions (session_id, cwd, messages, updated_at, message_count) VALUES ('old','/p',?,5,-1)",
        (json.dumps([{"role": "user", "content": "carried over"}]),),
    )
    row = "INSERT INTO sessions (session_id, cwd, messages, updated_at, message_count) VALUES (?,?,?,?,?)"
    db.execute(row, ("old-empty", "/p", "[]", 6, -1))
    # The count column is the source of truth: these transcripts are never looked at.
    db.execute(row, ("counted", "/p", "[]", 7, 3))
    db.execute(
        "INSERT INTO sessions (session_id, cwd, messages, updated_at, message_count) VALUES ('zero','/p',?,8,0)",
        (json.dumps([{"role": "user", "content": "stale body"}]),),
    )
    db.commit()
    ids = {sid for sid, _ in store.worked_in(limit=50)}
    assert ids == {"counted", "old", used}
    assert empty not in ids


@pytest.mark.serial  # wall-clock limit; load-sensitive under the 8-worker run, passes alone
def test_worked_in_does_not_read_the_transcripts(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    big = json.dumps([{"role": "user", "content": "x" * 2_000_000}])
    store._db.executemany(
        "INSERT INTO sessions (session_id, cwd, messages, updated_at, message_count, preview)"
        " VALUES (?,'/p',?,?,1,'x')",
        [(f"s{i:03d}", big, float(i)) for i in range(60)],
    )
    store._db.commit()
    # A fresh connection: the page cache of the writing connection would hide a full read.
    store = SessionStore(tmp_path / "s.db")
    began = time.perf_counter()
    assert len(store.worked_in(limit=2000)) == 60
    assert time.perf_counter() - began < 0.1  # 120 MB of transcripts take far longer to read
