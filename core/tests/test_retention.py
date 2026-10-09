"""Retention: what grew without bound under $K3CODE_HOME.

A session's journal (and transcript checkpoint) outlived session.delete and the empty-session sweep, usage.db was
append-only, debug bundles were never removed, and the decision log was read whole on every turn end."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from k3code import debugdump
from k3code.learning.decisions import DecisionLog
from k3code.usage import UsageDB
from test_autonomy_gateway import call, k3home, make, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}
REPLY = [{"type": "text", "text": "ok"}]
DAY = 86400.0


def _journal(home, sid: str, age_days: float = 0.0) -> list:
    jdir = home / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    paths = [jdir / f"{sid}.jsonl", jdir / f"{sid}.messages.json"]
    stamp = time.time() - age_days * DAY
    for p in paths:
        p.write_text("{}\n")
        os.utime(p, (stamp, stamp))
    return paths


async def _server(tmp_path, monkeypatch, **cfg):
    server = make(tmp_path, monkeypatch, REPLY, **NO_GATE, **cfg)
    await start(server, tmp_path)
    return server


async def test_session_delete_removes_its_journal_and_pastes(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    home = k3home(tmp_path)
    doomed = server.store.create(title="old work")
    kept = server.store.create(title="other")
    doomed_files = _journal(home, doomed.session_id)
    kept_files = _journal(home, kept.session_id)
    pastes = home / "pastes" / doomed.session_id
    pastes.mkdir(parents=True)
    (pastes / "abc.txt").write_text("pasted")
    assert (await call(server, "session.delete", {"session_id": doomed.session_id}))["deleted"] is True
    assert not any(p.exists() for p in doomed_files) and not pastes.exists()
    assert all(p.exists() for p in kept_files)


async def test_session_delete_never_turns_a_given_id_into_a_path(tmp_path, monkeypatch):
    """`..` made the pastes directory $K3CODE_HOME itself, and the removal took every file in it."""
    server = await _server(tmp_path, monkeypatch)
    home = k3home(tmp_path)
    pasted = Path((await call(server, "paste.collapse", {"text": "keep me"}))["path"])
    (home / "precious.txt").write_text("user data")
    for sid in ("..", ".", "../..", "/", "x/../.."):
        assert (await call(server, "session.delete", {"session_id": sid}))["deleted"] is False
    assert (home / "precious.txt").exists() and pasted.exists() and (tmp_path / "sessions.db").exists()


async def test_session_delete_of_a_live_session_closes_and_removes_its_journal(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    from test_autonomy_gateway import run_turn

    await run_turn(server, "hi")
    sid = server.session.session_id
    files = _journal(k3home(tmp_path), sid)
    await call(server, "session.delete", {"session_id": sid})
    assert not any(p.exists() for p in files)


async def test_the_empty_session_sweep_removes_the_swept_sessions_journals(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    server.automation = SimpleNamespace(references_session=lambda sid: False)  # no loop points at any row
    home = k3home(tmp_path)
    empty = server.store.create()
    worked = server.store.create(title="has a title")
    live = server.session.session_id
    empty_files, worked_files, live_files = (_journal(home, s) for s in (empty.session_id, worked.session_id, live))
    swept = await server.sweep_empty_sessions_async(now=time.time() + 31 * DAY)
    assert swept == 1 and server.store.get(empty.session_id) is None
    assert not any(p.exists() for p in empty_files)
    assert all(p.exists() for p in worked_files + live_files)


async def test_daemon_start_prunes_old_journals_of_closed_sessions_and_old_usage_rows(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    home = k3home(tmp_path)
    live = server.session.session_id
    old_closed = _journal(home, "closedold", age_days=31)
    new_closed = _journal(home, "closednew", age_days=5)
    old_live = _journal(home, live, age_days=40)
    (home / "journal" / "notes.txt").write_text("not a journal")
    now = time.time()
    server.usage.record("tool", detail="ancient", ts=now - 181 * DAY)
    server.usage.record("tool", detail="recent", ts=now - 179 * DAY)
    server.learning.log.record("approval", subject="last year", choice="allow", ts=now - 400 * DAY)
    server.learning.log.record("approval", subject="this year", choice="allow", ts=now - 100 * DAY)
    pruned = server.prune_retention()
    assert pruned == {"usage_rows": 1, "journal_files": 2, "decisions": 1}
    assert not any(p.exists() for p in old_closed)
    assert all(p.exists() for p in new_closed + old_live) and (home / "journal" / "notes.txt").exists()
    assert [r["detail"] for r in server.usage.rows() if r["kind"] == "tool"] == ["recent"]
    assert [r["subject"] for r in server.learning.log.query("approval")] == ["this year"]


async def test_retention_knobs_come_from_the_config(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch, retention={"usage_days": 10, "journal_days": 3})
    home = k3home(tmp_path)
    four_days = _journal(home, "closed", age_days=4)
    server.usage.record("tool", detail="eleven days", ts=time.time() - 11 * DAY)
    assert server.prune_retention() == {"usage_rows": 1, "journal_files": 2, "decisions": 0}
    assert not any(p.exists() for p in four_days)


def test_usage_prune(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    now = time.time()
    for age in (1, 100, 200, 400):
        db.record("call", detail=str(age), ts=now - age * DAY)
    assert db.prune(180, now=now) == 2
    assert sorted(r["detail"] for r in db.rows()) == ["1", "100"]
    db.close()


async def test_debug_dump_keeps_the_newest_ten_bundles(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    out = tmp_path / "dbg" / "debug"
    out.mkdir(parents=True)
    old = [out / f"20200101-0000{i:02d}.tar.gz" for i in range(12)]
    for p in old:
        p.write_bytes(b"x")
    path = await debugdump.write_bundle(server, 10, home=tmp_path / "dbg", probe=False)
    left = sorted(p.name for p in out.glob("*.tar.gz"))
    assert len(left) == 10 and path.name in left
    assert left[:9] == [p.name for p in old[-9:]]


# ── decision log ──


def test_decision_queries_filter_the_actor_in_sql_before_the_limit(tmp_path):
    log = DecisionLog(tmp_path)
    for i in range(3):
        log.record("approval", subject=f"auto {i}", choice="allow", actor="auto")
    for i in range(2):
        log.record("approval", subject=f"user {i}", choice="allow")
    # the actor was filtered in Python after LIMIT: two auto rows filled the limit and the user rows never came back
    assert [r["subject"] for r in log.query("approval", limit=2)] == ["user 0", "user 1"]
    assert len(log.query("approval", actor=None)) == 5
    assert [r["subject"] for r in log.query("approval", actor="auto")] == ["auto 0", "auto 1", "auto 2"]
    idx = {r[1] for r in log._db.execute("PRAGMA index_list(decisions)")}
    assert "decisions_kind_actor_ts" in idx
    plan = " ".join(
        str(r[3])
        for r in log._db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM decisions WHERE kind=? AND actor=? AND ts>=?", ("approval", "user", 0)
        )
    )
    assert "decisions_kind_actor_ts" in plan
    log.close()


def test_an_old_decision_log_gets_the_actor_column_from_its_detail(tmp_path):
    path = tmp_path / "learning" / "decisions.db"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,"
        " session TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',"
        " subject TEXT NOT NULL DEFAULT '', choice TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}')"
    )
    now = time.time()
    for subject, detail in (("a", {"actor": "auto"}), ("u", {"actor": "user"}), ("none", {})):
        db.execute(
            "INSERT INTO decisions (ts, kind, subject, detail) VALUES (?,?,?,?)",
            (now, "approval", subject, json.dumps(detail)),
        )
    db.commit()
    db.close()
    log = DecisionLog(tmp_path)
    assert [r["subject"] for r in log.query("approval")] == ["u", "none"]
    assert [r["subject"] for r in log.query("approval", actor="auto")] == ["a"]
    log.close()


def test_decision_prune_drops_rows_older_than_a_year(tmp_path):
    now = time.time()
    log = DecisionLog(tmp_path, clock=lambda: now)
    log.record("approval", subject="old", choice="allow", ts=now - 366 * DAY)
    log.record("approval", subject="recent", choice="allow", ts=now - 300 * DAY)
    assert log.prune() == 1
    assert [r["subject"] for r in log.query("approval")] == ["recent"]
    log.close()
