"""Tool-journal tests: intent/done round-trip, crash resume, torn lines."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.reliability.journal import (
    INTERRUPTED_TEMPLATE,
    ToolJournal,
    args_digest,
    interrupted_result,
    result_digest,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "k3home"


def test_intent_done_round_trip_leaves_nothing_pending(home: Path):
    j = ToolJournal(home, "s1")
    try:
        j.record_intent("c1", "read", {"path": "a.txt"}, side_effect=False)
        j.record_done("c1", {"content": "hi"})
        assert j.resume_plan() == []
        recs = j.read_all()
        assert [r.type for r in recs] == ["intent", "done"]
        assert recs[0].tool == "read"
        assert recs[0].side_effect is False
    finally:
        j.close()


def test_crash_after_bash_intent_synthesizes_interrupted(home: Path):
    """Side-effect tool with intent but no done -> INTERRUPTED, never re-run."""
    j = ToolJournal(home, "s2")
    try:
        j.record_intent("c9", "bash", {"cmd": "touch marker"}, side_effect=True)
    finally:
        j.close()  # simulate kill -9 before record_done

    j2 = ToolJournal(home, "s2")
    try:
        plan = j2.resume_plan()
        assert len(plan) == 1
        pending = plan[0]
        assert pending.id == "c9"
        assert pending.tool == "bash"
        assert pending.side_effect is True

        res = interrupted_result(pending.tool)
        assert res["interrupted"] is True
        assert "bash" in res["error"]
        assert res["error"] == INTERRUPTED_TEMPLATE.format(tool="bash")
    finally:
        j2.close()


def test_pure_tool_intent_carries_raw_args_for_rerun(home: Path):
    j = ToolJournal(home, "s3")
    try:
        args = {"path": "notes.txt"}
        j.record_intent("c2", "read", args, side_effect=False)
    finally:
        j.close()

    j2 = ToolJournal(home, "s3")
    try:
        plan = j2.resume_plan()
        assert len(plan) == 1
        assert plan[0].side_effect is False
        assert plan[0].args == {"path": "notes.txt"}
    finally:
        j2.close()


def test_done_without_intent_is_ignored(home: Path):
    j = ToolJournal(home, "s4")
    try:
        j.record_done("ghost", {"x": 1})
        assert j.resume_plan() == []
    finally:
        j.close()


def test_torn_last_line_is_skipped(home: Path):
    j = ToolJournal(home, "s5")
    try:
        j.record_intent("c1", "read", {"path": "a"}, side_effect=False)
        j.record_done("c1", {"ok": True})
    finally:
        j.close()
    # crash mid-write leaves a partial line
    with j.path.open("a", encoding="utf-8") as f:
        f.write('{"type": "intent", "id": "c2", "tool": "bas')

    j2 = ToolJournal(home, "s5")
    try:
        recs = j2.read_all()
        assert [r.id for r in recs] == ["c1", "c1"]
        assert j2.resume_plan() == []
    finally:
        j2.close()


def test_find_pending_ignores_completed_and_blank_lines(home: Path):
    j = ToolJournal(home, "s6")
    try:
        j.record_intent("a", "read", {"p": "1"}, side_effect=False)
        j.record_intent("b", "bash", {"cmd": "x"}, side_effect=True)
        j.record_done("a", {"ok": True})
    finally:
        j.close()
    with j.path.open("a", encoding="utf-8") as f:
        f.write("\n")  # blank line skipped

    j2 = ToolJournal(home, "s6")
    try:
        pending = j2.find_pending(j2.read_all())
        assert [r.id for r in pending] == ["b"]
        args_by_id = j2.load_args(j2.read_all())
        assert args_by_id["b"] == {"cmd": "x"}
    finally:
        j2.close()


def test_digest_stable_and_short(home: Path):
    assert args_digest({"b": 1, "a": 2}) == args_digest({"a": 2, "b": 1})
    d = result_digest({"content": "x" * 1000})
    assert len(d) == 16
    assert result_digest({"content": "x"}) == result_digest({"content": "x"})


def test_sessions_are_isolated_files(home: Path):
    j1 = ToolJournal(home, "alpha")
    j2 = ToolJournal(home, "beta")
    try:
        j1.record_intent("c1", "bash", {"cmd": "x"}, side_effect=True)
        assert j1.path != j2.path
        assert j2.resume_plan() == []
        assert len(j1.resume_plan()) == 1
    finally:
        j1.close()
        j2.close()
