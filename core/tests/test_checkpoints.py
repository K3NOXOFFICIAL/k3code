"""File checkpoints for /undo (k3code.checkpoints): what the agent's write/edit changed in a turn is rewound with it.

The module is imported inside each test, so the tests fail (rather than error at collection) without it."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from test_autonomy_gateway import call, make, run_turn, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path.parent / f"{tmp_path.name}-k3home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    return home


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    return project


def _agent_write(turn: Any, path: Path, data: bytes) -> str | None:
    """What the loop does around a write/edit: capture, change the file as the tools do, record."""
    from k3code import checkpoints
    from k3code.tools import _atomic_write

    prior = checkpoints.capture(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, data)
    return turn.record(prior)


def test_edit_then_undo_restores_the_exact_bytes(tmp_path: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    f = project / "a.bin"
    original = b"line one\r\n\x00\xff latin-1 \xe9\n"
    f.write_bytes(original)
    _agent_write(checkpoints.Turn("s1", 0), f, b"changed\n")
    rewind = checkpoints.restore("s1", 0, [str(project)])
    assert f.read_bytes() == original
    assert rewind.restored == [str(f)] and not rewind.conflicts
    assert "restored 1 file(s)" in rewind.report()["note"]


def test_a_file_the_agent_created_is_removed_on_undo(tmp_path: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    f = project / "new.txt"
    _agent_write(checkpoints.Turn("s1", 0), f, b"fresh\n")
    rewind = checkpoints.restore("s1", 0, [str(project)])
    assert not f.exists()
    assert rewind.deleted == [str(f)]


def test_two_edits_in_one_turn_restore_to_the_first_prior_state(tmp_path: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    f = project / "a.txt"
    f.write_bytes(b"v0\n")
    turn = checkpoints.Turn("s1", 4)
    _agent_write(turn, f, b"v1\n")
    _agent_write(turn, f, b"v2\n")
    rewind = checkpoints.restore("s1", 4, [str(project)])
    assert f.read_bytes() == b"v0\n"
    assert rewind.restored == [str(f)] and not rewind.conflicts


def test_rewind_to_turn_n_keeps_the_earlier_turns(tmp_path: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    a, b = project / "a.txt", project / "b.txt"
    a.write_bytes(b"a0\n")
    _agent_write(checkpoints.Turn("s1", 0), a, b"a1\n")
    _agent_write(checkpoints.Turn("s1", 2), a, b"a2\n")
    _agent_write(checkpoints.Turn("s1", 2), b, b"b2\n")
    _agent_write(checkpoints.Turn("s1", 4), a, b"a3\n")
    rewind = checkpoints.restore("s1", 2, [str(project)])
    assert a.read_bytes() == b"a1\n"  # back to what turn 0 left, not to before it
    assert not b.exists()
    assert sorted(rewind.restored + rewind.deleted) == sorted([str(a), str(b)])
    # turn 0 is still there: a later undo of it goes all the way back
    checkpoints.restore("s1", 0, [str(project)])
    assert a.read_bytes() == b"a0\n"


def test_a_file_changed_after_the_agent_wrote_it_is_a_conflict_left_untouched(tmp_path: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    f, other = project / "a.txt", project / "b.txt"
    f.write_bytes(b"original\n")
    other.write_bytes(b"other0\n")
    turn = checkpoints.Turn("s1", 0)
    _agent_write(turn, f, b"agent\n")
    _agent_write(turn, other, b"other1\n")
    f.write_bytes(b"the user's own edit\n")
    rewind = checkpoints.restore("s1", 0, [str(project)])
    assert f.read_bytes() == b"the user's own edit\n"
    assert [p for p, _ in rewind.conflicts] == [str(f)]
    assert other.read_bytes() == b"other0\n"  # the rest is still rewound
    note = rewind.report()["note"]
    assert "conflict" in note and str(f) in note


def test_restore_never_writes_through_a_link_to_outside_the_roots(tmp_path: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    sub = project / "sub"
    sub.mkdir()
    f = sub / "a.txt"
    f.write_bytes(b"original\n")
    _agent_write(checkpoints.Turn("s1", 0), f, b"agent\n")
    # the directory is swapped for a link to one outside every root holding exactly what the agent wrote: only the
    # path check, not the content check, can stop the restore from writing there
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "a.txt"
    victim.write_bytes(b"agent\n")
    os.rename(sub, project / "sub.moved")
    sub.symlink_to(outside)
    rewind = checkpoints.restore("s1", 0, [str(project)])
    assert victim.read_bytes() == b"agent\n"
    assert [p for p, _ in rewind.conflicts] == [str(f)]


def test_restore_never_writes_outside_the_roots_it_is_given(tmp_path: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    f = elsewhere / "a.txt"
    f.write_bytes(b"original\n")
    _agent_write(checkpoints.Turn("s1", 0), f, b"agent\n")
    rewind = checkpoints.restore("s1", 0, [str(project)])
    assert f.read_bytes() == b"agent\n"
    assert rewind.conflicts == [(str(f), "outside the session's project roots")]


def test_a_file_over_the_size_cap_is_skipped_with_a_notice_and_the_others_still_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from k3code import checkpoints

    monkeypatch.setattr(checkpoints, "MAX_FILE_BYTES", 16)
    project = _project(tmp_path)
    big, small = project / "big.txt", project / "small.txt"
    big.write_bytes(b"x" * 64)
    small.write_bytes(b"s0\n")
    turn = checkpoints.Turn("s1", 0)
    notice = _agent_write(turn, big, b"y" * 64)
    assert notice is not None and str(big) in notice and "/undo will not restore it" in notice
    assert _agent_write(turn, big, b"z" * 64) is None  # one notice per file and turn, not one per write
    assert _agent_write(turn, small, b"s1\n") is None
    rewind = checkpoints.restore("s1", 0, [str(project)])
    assert big.read_bytes() == b"z" * 64
    assert small.read_bytes() == b"s0\n"
    assert [p for p, _ in rewind.skipped] == [str(big)]
    assert "not restored 1 file(s)" in rewind.report()["note"]


def test_the_session_size_cap_skips_with_a_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code import checkpoints

    monkeypatch.setattr(checkpoints, "MAX_SESSION_BYTES", 10)
    project = _project(tmp_path)
    f = project / "a.txt"
    f.write_bytes(b"0123456789abcdef")
    notice = _agent_write(checkpoints.Turn("s1", 0), f, b"agent\n")
    assert notice is not None and "full" in notice


def test_retention_keeps_the_newest_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _home: Path) -> None:
    from k3code import checkpoints

    monkeypatch.setattr(checkpoints, "MAX_TURNS", 2)
    project = _project(tmp_path)
    f = project / "a.txt"
    f.write_bytes(b"v0\n")
    for i in (1, 2, 3):
        _agent_write(checkpoints.Turn("s1", i * 2), f, f"v{i}\n".encode())
    turns = sorted(p.name for p in (_home / "checkpoints" / "s1").iterdir() if p.is_dir())
    assert turns == ["4", "6"]  # turn 2 was evicted
    checkpoints.restore("s1", 0, [str(project)])
    assert f.read_bytes() == b"v1\n"  # only as far back as the oldest kept turn


def test_stored_checkpoints_are_private_whatever_the_umask(tmp_path: Path, _home: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    f = project / "a.txt"
    f.write_bytes(b"secret\n")
    old = os.umask(0)
    try:
        _agent_write(checkpoints.Turn("s1", 0), f, b"agent\n")
    finally:
        os.umask(old)
    root = _home / "checkpoints" / "s1"
    stored = [p for p in root.rglob("*") if p.is_file()]
    assert len(stored) == 2  # the index and the blob
    for p in stored:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    for d in (_home / "checkpoints", root, *(p for p in root.iterdir() if p.is_dir())):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_a_session_id_is_never_a_path(tmp_path: Path, _home: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    f = project / "a.txt"
    _agent_write(checkpoints.Turn("../escape", 0), f, b"agent\n")
    assert not (_home / "escape").exists() and not (_home / "checkpoints").exists()
    assert not checkpoints.restore("../escape", 0, [str(project)])


# ── wired: the loop records, session.undo rewinds ──────────────────────────────────────────────────────────


def _write_then_answer(path: str, content: str) -> list[dict]:
    return [
        {
            "type": "tool_call",
            "when": "turn_first",
            "id": "w1",
            "name": "write",
            "arguments": {"path": path, "content": content},
        },
        {"type": "text", "when": "turn_after_tool", "text": "written"},
    ]


async def test_undo_removes_the_file_the_turn_wrote_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = make(tmp_path, monkeypatch, _write_then_answer("made.txt", "hi\n"), mode="yolo", **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "make a file")
    made = tmp_path / "made.txt"
    assert made.read_text() == "hi\n"
    res = await call(server, "session.undo", {})
    assert res["removed"] >= 2
    assert not made.exists()
    assert res["deleted"] == [str(made)]
    assert str(made) in res["note"]


async def test_undo_rewinds_only_the_undone_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    steps = [  # the first turn writes the file; the second only answers
        {
            "type": "tool_call",
            "when": "first",
            "id": "w1",
            "name": "write",
            "arguments": {"path": "a.txt", "content": "a"},
        },
        {"type": "text", "when": "after_tool", "text": "ok"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="yolo", **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "write it")
    await run_turn(server, "and now?")
    made = tmp_path / "a.txt"
    assert await call(server, "session.undo", {}) == {"removed": 2}
    assert made.read_text() == "a"
    res = await call(server, "session.undo", {})
    assert not made.exists() and res["deleted"] == [str(made)]


async def test_undo_restores_a_file_the_turn_overwrote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "f.txt"
    f.write_bytes(b"before\r\n")
    server = make(tmp_path, monkeypatch, _write_then_answer("f.txt", "after\n"), mode="yolo", **NO_GATE)
    await start(server, tmp_path)
    server.session.file_stamps.record(f)  # as if the model had read it
    await run_turn(server, "overwrite it")
    assert f.read_bytes() == b"after\n"
    res = await call(server, "session.undo", {})
    assert f.read_bytes() == b"before\r\n"
    assert res["restored"] == [str(f)]


async def test_undo_without_checkpoints_only_truncates_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = make(tmp_path, monkeypatch, [{"type": "text", "text": "hello back"}], **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "hi")
    assert await call(server, "session.undo", {}) == {"removed": 2}


async def test_undo_of_a_turn_that_ran_bash_says_shell_changes_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from k3code.reliability import sandbox

    monkeypatch.setattr(sandbox, "should_sandbox", lambda *a, **k: False)
    steps = [
        {"type": "tool_call", "when": "turn_first", "id": "b1", "name": "bash", "arguments": {"command": "echo hi"}},
        {"type": "text", "when": "turn_after_tool", "text": "ran it"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="yolo", **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "run something")
    res = await call(server, "session.undo", {})
    assert res["removed"] >= 2
    assert "shell commands ran" in res["note"] and "not restored" in res["note"]
    assert res["restored"] == [] and res["deleted"] == []


async def test_undo_after_clear_does_not_rewind_a_file_the_user_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After /clear the turn indexes start over: an /undo that found the old turn's key would silently put back a file
    the user kept."""
    steps = [
        {
            "type": "tool_call",
            "when": "first",
            "id": "w1",
            "name": "write",
            "arguments": {"path": "kept.txt", "content": "k"},
        },
        {"type": "text", "when": "after_tool", "text": "ok"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="yolo", **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "make a file")
    kept = tmp_path / "kept.txt"
    assert kept.read_text() == "k"
    await server.dispatch_command("clear", "", server.session.session_id)
    await run_turn(server, "and now?")  # a turn that edits nothing, at the same message index as the first
    await call(server, "session.undo", {})
    assert kept.read_text() == "k"


def test_a_rewritten_conversation_drops_the_checkpoints(tmp_path: Path, _home: Path) -> None:
    """/compact and /clear both settle the turns through one helper: the checkpoints go with them."""
    from types import SimpleNamespace

    from k3code import checkpoints
    from k3code.commands.builtin import _settle_turns

    project = _project(tmp_path)
    f = project / "a.txt"
    f.write_bytes(b"before\n")
    _agent_write(checkpoints.Turn("s1", 10), f, b"after\n")
    _settle_turns(SimpleNamespace(learning=None), SimpleNamespace(session_id="s1"))
    rewind = checkpoints.restore("s1", 0, [str(project)])
    assert not rewind and f.read_bytes() == b"after\n"


def test_the_report_says_when_sub_agents_ran_because_their_edits_are_not_checkpointed() -> None:
    from k3code import checkpoints

    note = checkpoints.Rewind().report(delegated=True)["note"]
    assert "sub-agents ran" in note and "not restored" in note
    assert "sub-agents" not in checkpoints.Rewind().report(shell=True)["note"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory")
def test_a_restore_that_cannot_write_keeps_the_earlier_bytes(tmp_path: Path, _home: Path) -> None:
    from k3code import checkpoints

    project = _project(tmp_path)
    f = project / "a.txt"
    f.write_bytes(b"original\n")
    _agent_write(checkpoints.Turn("s1", 0), f, b"changed\n")
    f.chmod(0o444)
    project.chmod(0o555)  # neither a temporary file beside it nor the file itself can be written
    try:
        rewind = checkpoints.restore("s1", 0, [str(project)])
    finally:
        project.chmod(0o755)
        f.chmod(0o644)
    assert f.read_bytes() == b"changed\n" and not rewind.restored
    ((path, why),) = rewind.conflicts
    kept = Path(why.rsplit(" ", 1)[-1])
    assert path == str(f) and "earlier version is kept at" in why
    assert kept.read_bytes() == b"original\n"  # the checkpoints are gone, this copy is not
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
