"""Audit fixes: goal loops that never end, the halt flag, blockers retention."""

from __future__ import annotations

import json
import sqlite3

import pytest

from k3code import halt as halt_mod
from k3code.blockers import BlockerStore
from k3code.goals import GoalManager, make_judge

MAX_CONSECUTIVE_EMPTY = 3  # k3code.goals.MAX_CONSECUTIVE_EMPTY


def _mgr(**kw) -> GoalManager:
    box: dict[str, dict | None] = {"goal": None}
    return GoalManager(lambda: box["goal"], lambda s: box.__setitem__("goal", s), **kw)


async def _never(system: str, user: str) -> str:
    raise AssertionError("an empty reply is not sent to the judge model")


# ── M6: empty replies ──


async def test_a_goal_whose_replies_are_empty_pauses_instead_of_looping_forever():
    mgr = _mgr()
    mgr.set("do the thing")
    judge = make_judge(_never)
    decisions = []
    for _ in range(10):
        decisions.append(await mgr.evaluate_after_turn("  \n", judge))
        if not decisions[-1].should_continue:
            break
    assert len(decisions) == MAX_CONSECUTIVE_EMPTY and not decisions[-1].should_continue
    st = mgr.state
    assert st.status == "paused" and "no text" in st.paused_reason and str(MAX_CONSECUTIVE_EMPTY) in st.paused_reason
    assert "paused" in decisions[-1].message


async def test_one_real_reply_resets_the_empty_count_and_resume_does_too():
    mgr = _mgr()
    mgr.set("do the thing")
    judge = make_judge(_never)
    await mgr.evaluate_after_turn("", judge)
    await mgr.evaluate_after_turn("", judge)
    assert mgr.state.consecutive_empty_replies == 2

    async def judged(system: str, user: str) -> str:
        return '{"verdict": "continue", "reason": "more to do"}'

    ok = await mgr.evaluate_after_turn("did a step", make_judge(judged))
    assert ok.should_continue and mgr.state.consecutive_empty_replies == 0
    for _ in range(MAX_CONSECUTIVE_EMPTY):
        await mgr.evaluate_after_turn("", judge)
    assert mgr.state.status == "paused"
    mgr.resume()
    assert mgr.state.status == "active" and mgr.state.consecutive_empty_replies == 0


async def test_an_implicit_goal_with_empty_replies_ends_quietly():
    mgr = _mgr()
    mgr.set("answer", implicit=True)
    judge = make_judge(_never)
    last = None
    for _ in range(MAX_CONSECUTIVE_EMPTY):
        last = await mgr.evaluate_after_turn("", judge)
    assert last is not None and not last.should_continue and mgr.state is None


# ── M7: the reviewer's veto when the turn budget is spent ──


async def test_a_reviewer_veto_on_the_last_turn_does_not_report_the_goal_achieved():
    mgr = _mgr()
    mgr.set("ship it", max_turns=1)

    async def done(goal: str, response: str):
        return "done", "looks finished", False, False

    async def reviewer(goal: str):
        return True, ["the tests do not run"]

    d = await mgr.evaluate_after_turn("all done", done, reviewer=reviewer)
    assert d.status == "paused" and not d.should_continue and "achieved" not in d.message
    st = mgr.state
    assert st.status == "paused" and st.last_verdict == "advisor_blocked" and "tests do not run" in st.last_reason
    assert "turn budget" in st.paused_reason


async def test_a_reviewer_veto_with_turns_left_still_continues_and_no_veto_is_done():
    mgr = _mgr()
    mgr.set("ship it", max_turns=5)

    async def done(goal: str, response: str):
        return "done", "looks finished", False, False

    async def veto(goal: str):
        return True, ["fix X"]

    async def fine(goal: str):
        return False, []

    d = await mgr.evaluate_after_turn("all done", done, reviewer=veto)
    assert d.should_continue and d.verdict == "advisor_blocked"
    assert (await mgr.evaluate_after_turn("fixed", done, reviewer=fine)).status == "done"


# ── L4: halt.json ──


def _write(home, text: str) -> None:
    halt_mod.halt_path(home).write_text(text)


def test_a_bad_since_does_not_crash_load_halt_and_the_halt_still_holds(tmp_path):
    _write(tmp_path, json.dumps({"active": True, "reason": "storm", "since": "yesterday"}))
    h = halt_mod.load_halt(tmp_path)
    assert h is not None and h.reason == "storm" and h.since == 0.0


@pytest.mark.parametrize("junk", ['{"active": tru', "[1, 2]", "null", '"halt"', "\x00\x01 garbage"])
def test_a_damaged_halt_file_fails_closed_and_resume_clears_it(tmp_path, junk):
    _write(tmp_path, junk)
    h = halt_mod.load_halt(tmp_path)
    assert h is not None and "/daemon resume" in h.reason
    assert halt_mod.clear_halt(tmp_path) is True
    assert halt_mod.load_halt(tmp_path) is None and not halt_mod.halt_path(tmp_path).exists()


def test_no_file_an_empty_file_an_inactive_file_and_a_directory_are_not_halts(tmp_path):
    assert halt_mod.load_halt(tmp_path) is None
    _write(tmp_path, "  \n")
    assert halt_mod.load_halt(tmp_path) is None
    _write(tmp_path, json.dumps({"active": False, "reason": "old"}))
    assert halt_mod.load_halt(tmp_path) is None
    halt_mod.halt_path(tmp_path).unlink()
    halt_mod.halt_path(tmp_path).mkdir()
    assert halt_mod.load_halt(tmp_path) is None  # not our flag, and nothing could remove it: no lock-out


def test_a_halt_written_by_set_halt_round_trips(tmp_path):
    halt_mod.set_halt(tmp_path, "pause", now=12.5)
    assert halt_mod.load_halt(tmp_path) == halt_mod.Halt(reason="pause", since=12.5)


# ── L7: blockers.db ──


def test_blockers_has_an_index_for_pending_and_prunes_old_delivered_rows(tmp_path):
    path = tmp_path / "blockers.db"
    store = BlockerStore(path)
    old = store.add(session_id="s", kind="approval", text="old delivered", now=100.0)
    kept_pending = store.add(session_id="s", kind="approval", text="old pending", now=100.0)
    import time

    recent = store.add(session_id="s", kind="approval", text="recent delivered", now=time.time())
    store.mark_delivered(old, now=200.0)
    store.mark_delivered(recent, now=time.time())
    store.close()

    store = BlockerStore(path)  # opening prunes
    assert [b["id"] for b in store.pending()] == [kept_pending]
    ids = {r[0] for r in store._db.execute("SELECT id FROM blockers")}
    assert ids == {kept_pending, recent}
    store.close()
    db = sqlite3.connect(path)
    plan = " ".join(
        str(r) for r in db.execute("EXPLAIN QUERY PLAN SELECT * FROM blockers WHERE delivered_at IS NULL ORDER BY id")
    )
    assert "blockers_pending" in plan
    db.close()
