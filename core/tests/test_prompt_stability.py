"""The system prompt stays one prefix for a session while learning writes in the background (k3code #161, finding 1).

The gateway rebuilt the prompt from disk for every loop, and its tail holds what the background writers change
mid-session: the review at user turn 7, 13, 19... rewrites learned.md, the daily distill USER.md's auto section. Each
write changed the prompt's bytes, so the provider's cache missed from there on. The session now keeps the tail as its
first prompt read it, and reads it again when the conversation is rewritten anyway (compaction, /clear), when it moves
to another project, or when the user changes it on purpose (/memory add, an accepted card)."""

from __future__ import annotations

import json
from pathlib import Path

from k3code.config import Settings
from k3code.memory import learned_notes_path, read_learned, user_memory_path, write_learned
from k3code.prompting import build_system_prompt
from k3code.routing.tiers import TaskKind
from learn_helpers import FakeCaller
from m1cmd_helpers import cmd, make_server, new_session, rpc

FACT = "the integration tests need `make db` first"
REPLY = json.dumps({"facts": [FACT], "skills": []})


def exchange(i: int) -> list[dict]:
    return [{"role": "user", "content": f"request {i}"}, {"role": "assistant", "content": f"answer {i}"}]


async def started(tmp_path: Path, monkeypatch):
    """A session with one learned note already on disk, the background review answering with a new one."""
    server, _ = make_server(tmp_path, monkeypatch)
    server.learning._set_state(distilled=server.learning.clock())  # the daily distill is done: no other model call
    server.model_caller = FakeCaller(REPLY)  # type: ignore[assignment]
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    write_learned(repo, ["the project uses uv"])
    sid = await new_session(server, repo)
    return server, server.live[sid], repo


def prompt_of(server, live) -> str:
    """The system prompt the session's next loop sends (what _run_one_turn builds for each turn and tier)."""
    return server._build_loop(live, None, server.router, TaskKind.INTERACTIVE_TURN, None).system_prompt


async def finish_turns(server, live, start: int, count: int) -> None:
    for i in range(start, start + count):
        live.stored.messages.extend(exchange(i))
        await server.learning.turn_finished(live, "done")


async def test_the_review_at_user_turn_7_does_not_change_the_sessions_prompt(tmp_path, monkeypatch):
    server, live, repo = await started(tmp_path, monkeypatch)
    try:
        await finish_turns(server, live, 0, 1)
        first = prompt_of(server, live)
        assert "the project uses uv" in first

        await finish_turns(server, live, 1, 6)  # user turns 2 to 7: the review runs at the 7th
        assert server.model_caller.calls, "no review ran at user turn 7"
        assert FACT in read_learned(repo)  # it wrote the note to disk

        for _ in range(3):  # every later turn, and the escalated tier's loop within a turn
            assert prompt_of(server, live) == first
        # without the session's snapshot the same inputs give a different prompt: the cache break this test guards
        fresh = build_system_prompt(
            live.system_prompt, cwd=live.perms.cwd, config=server.config, mcp=server.mcp, session_id=live.session_id
        )
        assert FACT in fresh and fresh != first
    finally:
        await server.close()


async def test_a_distill_of_user_md_waits_for_the_next_compaction(tmp_path, monkeypatch):
    server, live, _repo = await started(tmp_path, monkeypatch)
    try:
        first = prompt_of(server, live)
        path = user_memory_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## Learned preferences (auto)\n\n- answers in German\n", encoding="utf-8")
        assert prompt_of(server, live) == first

        live.stored.messages.extend(m for i in range(40) for m in exchange(i))
        server.model_caller = FakeCaller("the work so far")  # type: ignore[assignment]
        assert await server._maybe_compact(live, force=True)
        after = prompt_of(server, live)
        assert after != first and "answers in German" in after
        assert prompt_of(server, live) == after  # and it is the new stable prefix
    finally:
        await server.close()


async def test_clear_reads_the_learned_notes_again(tmp_path, monkeypatch):
    server, live, repo = await started(tmp_path, monkeypatch)
    try:
        first = prompt_of(server, live)
        write_learned(repo, ["the project uses uv", FACT])
        assert prompt_of(server, live) == first

        await cmd(server, "/clear", live.session_id)
        assert FACT in prompt_of(server, live)
    finally:
        await server.close()


async def test_memory_add_applies_at_once(tmp_path, monkeypatch):
    server, live, repo = await started(tmp_path, monkeypatch)
    try:
        prompt_of(server, live)
        await cmd(server, "/memory add likes tea --user", live.session_id)
        assert "likes tea" in prompt_of(server, live)
        await cmd(server, "/memory add always use tabs", live.session_id)
        assert "always use tabs" in prompt_of(server, live)
    finally:
        await server.close()


async def test_an_accepted_gotcha_card_applies_at_once_and_another_kind_keeps_the_snapshot(tmp_path, monkeypatch):
    from k3code.autonomy.proposals import Proposal
    from k3code.learning.decisions import project_id

    server, live, repo = await started(tmp_path, monkeypatch)
    try:
        first = prompt_of(server, live)
        write_learned(repo, ["the project uses uv", FACT])  # a background write, still waiting for its compaction
        other = Proposal(id="n1", kind="note", text="a note", action="", key="n1")  # its handler writes nothing
        assert await server.learning.apply(other, live) == "noted"
        assert prompt_of(server, live) == first

        line = "run `make db` before the integration tests"
        payload = {"project": project_id(str(repo)), "line": line}
        card = Proposal(id="g1", kind="project_gotcha", text=line, action="", key="g1", payload=payload)
        await server.learning.apply(card, live)
        assert line in prompt_of(server, live)
    finally:
        await server.close()


async def test_a_project_move_reads_the_new_projects_notes(tmp_path, monkeypatch):
    server, live, _repo = await started(tmp_path, monkeypatch)
    try:
        prompt_of(server, live)
        other = tmp_path / "other"
        (other / ".git").mkdir(parents=True)
        write_learned(other, ["the other project uses make"])
        res = await rpc(server, "session.workspace.move", {"path": str(other)})
        assert res["result"]["ok"]
        assert live.prompt_frozen == {}
        live.perms.cwd = other  # where the prompt reads its directory from
        assert "the other project uses make" in prompt_of(server, live)
    finally:
        await server.close()


def test_a_snapshot_is_taken_again_when_its_inputs_change(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    write_learned(repo, ["one"])
    frozen: dict = {}
    on = build_system_prompt("BASE", cwd=repo, config=Settings(), frozen=frozen)
    write_learned(repo, ["one", "two"])
    assert build_system_prompt("BASE", cwd=repo, config=Settings(), frozen=frozen) == on
    off = build_system_prompt("BASE", cwd=repo, config=Settings(learning={"enabled": False}), frozen=frozen)
    assert "one" not in off.split("BASE", 1)[1]  # learning turned off: the learned notes leave at once
    assert learned_notes_path(repo).is_file()
