"""Issue #140: the improvement engine scores a turn by what happened to it, not by whether it finished.

A turn succeeded when it finished, was not undone (/undo, /retry before the next prompt) and not stopped by the user,
its last test/check did not fail and the goal judge did not call it blocked. Its outcome waits for a possible /undo
until the session's next prompt, its end or ``decisions.OUTCOME_SETTLE_S``; pending turns are not scored.
"""

from __future__ import annotations

import asyncio
import sqlite3

from k3code.goals import GoalManager, TurnEvidence
from k3code.learning import decisions, optimizer
from k3code.learning.decisions import DecisionLog
from learn_helpers import FakeClock
from test_learning_gateway import make_server
from test_permissions_gateway import bash, call, run_turn

NO_USAGE: list[dict] = []


def outcome(log, turn, session="s1", start=0, end=4, **fields):
    fields = {"status": "done", **fields}
    log.record_turn(turn, ts=log.clock(), session=session, msg_start=start, msg_end=end, **fields)


# ── the rule ──


def test_a_turn_passes_only_when_finished_kept_unstopped_tested_green_and_not_blocked():
    ok = {"status": "done", "interrupted": 0, "undone": 0, "test": None, "gate": None, "judge": ""}
    assert decisions.turn_passed(ok) is True
    assert decisions.turn_passed({**ok, "test": "passed", "judge": "done"}) is True
    assert decisions.turn_passed({**ok, "test": "unknown"}) is True  # a masked exit status says nothing either way
    for failed in (
        {"undone": 1},
        {"interrupted": 1, "status": "interrupted"},
        {"status": "error"},
        {"status": "needs_input"},
        {"test": "failed"},
        {"test": "passed", "gate": "failed"},  # the goal's --check ran after the turn's own test: it is the last
        {"judge": "blocked"},
    ):
        assert decisions.turn_passed({**ok, **failed}) is False, failed
    assert decisions.turn_passed({**ok, "test": "failed", "gate": "passed"}) is True  # the check after it passed
    for unscored in ({"status": "outage"}, {"status": "halted"}, {"status": "discarded"}, {"status": "interrupted"}):
        # an interrupt nobody asked for (a daemon stop) says nothing about the work either
        assert decisions.turn_passed({**ok, **unscored}) is None, unscored


def test_an_outcome_is_pending_until_the_session_moves_on_or_it_ages_out(tmp_path):
    clock = FakeClock()
    log = DecisionLog(tmp_path, clock)
    outcome(log, "t1")
    (row,) = log.turn_outcomes()
    assert row["pending"] is True and row["passed"] is None
    assert log.settle_turns("s1") == 1
    (row,) = log.turn_outcomes()
    assert row["pending"] is False and row["passed"] is True
    outcome(log, "t2", session="s2")
    clock.advance(decisions.OUTCOME_SETTLE_S + 1)  # nobody came back to undo it
    assert [r["passed"] for r in log.turn_outcomes()] == [True, True]


def test_an_undo_marks_the_pending_turn_whose_messages_it_removes(tmp_path):
    log = DecisionLog(tmp_path, FakeClock())
    outcome(log, "t1", start=0, end=4)
    log.settle_turns("s1")  # the user moved on: t1 can no longer be undone by a later cut
    outcome(log, "t2", start=4, end=9)
    assert log.mark_undone("s1", 2) is None  # a cut into the settled turn marks nothing
    assert log.mark_undone("s1", 4) == "t2"
    rows = {r["turn"]: r for r in log.turn_outcomes()}
    assert rows["t2"]["undone"] == 1 and rows["t2"]["passed"] is False and rows["t1"]["passed"] is True


def test_an_existing_decisions_db_gets_the_outcome_table_and_missing_columns(tmp_path):
    db_path = tmp_path / "learning" / "decisions.db"
    db_path.parent.mkdir(parents=True)
    old = sqlite3.connect(db_path)
    old.executescript(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,"
        " session TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',"
        " subject TEXT NOT NULL DEFAULT '', choice TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}');"
        "INSERT INTO decisions (ts, kind) VALUES (1, 'interrupt');"
        # a table an earlier build made with fewer columns
        "CREATE TABLE turn_outcomes (turn TEXT PRIMARY KEY, ts REAL NOT NULL DEFAULT 0, status TEXT NOT NULL"
        " DEFAULT '');"
        "INSERT INTO turn_outcomes (turn, ts, status) VALUES ('old', 1, 'done');"
    )
    old.commit()
    old.close()
    log = DecisionLog(tmp_path, FakeClock())
    assert log.count("interrupt") == 1  # the decisions survive
    outcome(log, "t1", test="failed", denials=2)
    log.settle_turns("s1")
    rows = {r["turn"]: r for r in log.turn_outcomes()}
    assert rows["t1"]["test"] == "failed" and rows["t1"]["denials"] == 2 and rows["t1"]["passed"] is False
    assert rows["old"]["undone"] == 0 and rows["old"]["passed"] is True  # defaults fill the old row
    DecisionLog(tmp_path, FakeClock())  # opening again is a no-op


def test_the_last_test_status_comes_from_the_turn_evidence():
    assert TurnEvidence().last_status() is None
    assert TurnEvidence(checks=[(1, "pytest", 1, True), (3, "pytest", 0, True)]).last_status() == "passed"
    assert TurnEvidence(checks=[(1, "pytest", 0, True), (3, "pytest", 2, True)]).last_status() == "failed"
    assert TurnEvidence(checks=[(1, "pytest", None, True)]).last_status() == "failed"  # the call itself errored
    assert TurnEvidence(checks=[(1, "pytest | tail", 0, False)]).last_status() == "unknown"


async def test_the_goal_decision_carries_the_check_result(monkeypatch):
    import k3code.goals as goals

    results = iter([(False, 1, "boom"), (True, 0, "")])

    async def gate(g, *, cwd=None):
        return next(results)

    monkeypatch.setattr(goals, "run_gate", gate)
    box: dict = {"goal": None}
    mgr = GoalManager(lambda: box["goal"], lambda s: box.__setitem__("goal", s))

    async def done(goal, response, evidence):
        return "done", "claims done", False, False

    mgr.set("ship", check="make test")
    assert (await mgr.evaluate_after_turn("x", done)).gate == "failed"
    assert (await mgr.evaluate_after_turn("x", done)).gate == "passed"
    mgr.set("ship")
    assert (await mgr.evaluate_after_turn("x", done)).gate is None  # no check configured


# ── the score ──


def test_the_success_rate_skips_pending_turns_and_says_why_the_others_failed(tmp_path):
    log = DecisionLog(tmp_path, FakeClock())
    outcome(log, "ok")
    outcome(log, "undone", undone=True)
    outcome(log, "red", test="failed")
    outcome(log, "stopped", status="interrupted", interrupted=True)
    outcome(log, "down", status="outage")
    log.settle_turns("s1")
    outcome(log, "waiting")  # still pending: not scored
    m = optimizer.collect(NO_USAGE, log, [], since=0)
    assert m["turn_success_rate"] == 0.25 and m["turns_scored"] == 4 and m["turns_pending"] == 1
    assert m["turn_failures"] == {"interrupted": 1, "undone": 1, "unfinished": 0, "test_failed": 1, "blocked": 0}
    line = optimizer.describe_success(m)
    assert "turn success 25% of 4 scored turns, 1 pending" in line and "undone 1" in line


def test_a_session_whose_turns_were_undone_scores_lower_than_one_that_kept_them():
    calls = {"calls": 10, "escalated": 0, "loop_guard": 0, "approvals": 0, "tokens": 0, "turns": 4}
    kept = optimizer.session_score({**calls, "scored": 4, "passed": 4}, None)
    undone = optimizer.session_score({**calls, "scored": 4, "passed": 1}, None)
    assert kept - undone == 0.75 * optimizer.SUCCESS_WEIGHT
    assert optimizer.session_score({**calls}, None) == kept  # a session stored before outcomes: as it scored before


async def test_an_undone_turn_counted_as_a_success_before_and_as_a_failure_now(tmp_path, monkeypatch):
    """The issue's example: a turn the user /undid right away. The old signal was ``passed = status == "done"``
    (the replay record) and turn completion (the score); both said success."""
    server, _ = make_server(tmp_path, ["here is a wrong answer", "a better answer"], monkeypatch)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    hub = server.learning
    await run_turn(server, "do the thing", [])
    await hub.drain()
    (record,) = hub.replays.load()
    old_passed = record["status"] == "done"  # what build_record wrote as "passed" before this change
    assert old_passed is True

    assert (await call(server, "session.undo", {}))["removed"] == 2
    (record,) = hub.replays.load()
    assert record["passed"] is False and record["outcome"]["undone"] == 1
    (row,) = hub.log.turn_outcomes(since=0)
    assert row["undone"] == 1 and row["passed"] is False
    assert hub.log.query("undo")[0]["subject"] == record["turn"]

    # the next prompt is kept: it settles as a success; the score counts one of two turns
    await run_turn(server, "do the thing", [])
    await hub.drain()
    now = hub.log.clock
    hub.log.clock = lambda: now() + decisions.OUTCOME_SETTLE_S + 1  # nobody came back to undo it
    m = hub.metrics(since=0)
    assert m["turn_success_rate"] == 0.5 and m["turns_scored"] == 2 and m["turn_failures"]["undone"] == 1
    await server.close()


async def test_a_turn_stays_pending_until_the_next_prompt_then_counts_as_kept(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["one", "two"], monkeypatch)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    hub = server.learning
    await run_turn(server, "first", [])
    (row,) = hub.log.turn_outcomes(since=0)
    assert row["pending"] is True and hub.metrics(since=0)["turns_scored"] == 0
    await run_turn(server, "second", [])
    first, second = hub.log.turn_outcomes(since=0)
    assert first["pending"] is False and first["passed"] is True and second["pending"] is True
    # an /undo now removes the second turn only
    await call(server, "session.undo", {})
    first, second = hub.log.turn_outcomes(since=0)
    assert first["passed"] is True and second["undone"] == 1 and second["passed"] is False
    await hub.drain()
    await server.close()


async def test_a_failing_test_and_a_denied_approval_are_recorded_on_the_turn(tmp_path, monkeypatch):
    runner = tmp_path / "pytest"  # a test runner that fails, at command position: what turn_evidence counts
    runner.write_text("#!/bin/sh\necho '1 failed'\nexit 1\n")
    runner.chmod(0o755)
    turns = [bash("./pytest -q", "c1"), bash("echo cleanup > x.txt", "c2"), "the tests pass"]
    server, _ = make_server(tmp_path, turns, monkeypatch)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_turn(server, "fix it", [{"choice": "once"}, {"choice": "deny"}])
    await server.learning.drain()
    (row,) = server.learning.log.turn_outcomes(since=0)
    assert row["status"] == "done" and row["test"] == "failed" and row["denials"] == 1
    server.learning.settle_turns(server.session.session_id)
    (row,) = server.learning.log.turn_outcomes(since=0)
    assert row["passed"] is False  # "the tests pass", said the model; the evidence says otherwise
    await server.close()


async def test_the_users_stop_is_recorded_as_interrupted(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["done"], monkeypatch)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    live = server.session
    started = asyncio.Event()

    async def hang(session, text):
        started.set()
        await asyncio.Event().wait()

    server.autonomy.prepare = hang  # type: ignore[method-assign]
    await call(server, "prompt.submit", {"text": "long task"})
    task = live.turn_task
    await asyncio.wait_for(started.wait(), 10)
    assert await server.interrupt_turn(live.session_id)
    await asyncio.gather(task, return_exceptions=True)
    (row,) = server.learning.log.turn_outcomes(since=0)
    assert row["interrupted"] == 1 and row["status"] == "interrupted"
    server.learning.settle_turns(live.session_id)
    assert server.learning.log.turn_outcomes(since=0)[0]["passed"] is False
    await server.learning.drain()
    await server.close()
