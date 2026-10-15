"""M1 P6-1: the replay harness stores per-turn inputs and outcomes and replays them deterministically."""

from __future__ import annotations

import asyncio
import json

from k3code.learning import replay
from k3code.learning.replay import (
    HISTORY_BIG_KEEP,
    SKILLS_CANDIDATE,
    Candidate,
    ReplayStore,
    auto_ok,
    build_record,
    evaluate_sync,
    tier_pass_delta,
)
from k3code.providers.types import Message, ToolCall
from k3code.skills import PROMPT_LIMIT


def turn(
    i: int,
    *,
    tool_chars: int = 200,
    tier: str = "main",
    status: str = "done",
    kind: str = "interactive_turn",
    tools: int = 1,
    memory: int = 500,
    skills: list[int] | None = None,
    user: str | None = None,
    tool: str = "grep",  # a tool the clip applies to: read keeps its own budget
):
    msgs = [Message(role="system", content="system " + "s" * 100), Message(role="user", content=user or f"task {i}")]
    for k in range(tools):
        msgs.append(
            Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id=f"c{k}", name=tool, arguments={"path": f"f{k}.py"})],
            )
        )
        msgs.append(Message(role="tool", content="x" * tool_chars, tool_call_id=f"c{k}", name=tool))
    msgs.append(Message(role="assistant", content=f"answer {i}"))
    return build_record(
        turn=f"t{i}",
        session="s1",
        kind=kind,
        tier=tier,
        status=status,
        messages=msgs,
        first=1,
        tokens_in=0,
        tokens_out=0,
        memory_chars=memory,
        skill_lines=skills or [80] * 5,
        ts=float(i),
    )


def test_replaying_the_same_records_gives_identical_token_totals():
    turns = [turn(i, tool_chars=8000) for i in range(20)]
    first = evaluate_sync(turns, Candidate("clip", clip_chars=1000))
    second = evaluate_sync(turns, Candidate("clip", clip_chars=1000))
    assert first == second
    assert first["tokens_before"] > first["tokens_after"] and first["requests"] == 40


def test_clip_candidate_reports_the_token_reduction_and_the_pass_bound():
    turns = [turn(i, tool_chars=8000) for i in range(10)] + [turn(10, tool_chars=100)]
    result = evaluate_sync(turns, Candidate("clip", clip_chars=1000))
    assert result["token_reduction_pct"] > 0 and result["changed_turns"] == 10
    assert result["pass_rate_before"] == 1.0 and result["pass_drop_points"] == round(100 * 10 / 11, 2)


def test_the_clip_candidate_leaves_read_results_to_their_own_budget():
    # the wire sends a read result whole up to its own budget, whatever tool_output_chars says: so does the replay
    reads = [turn(i, tool_chars=20_000, tool="read") for i in range(4)]
    assert reads[0]["entries"][2] == {"r": "tool", "n": 20_000, "t": "read"}
    result = evaluate_sync(reads, Candidate("clip", clip_chars=1000))
    assert result["tokens_before"] == result["tokens_after"] and result["changed_turns"] == 0
    # other tools are clipped, and so are results recorded before the tool name was kept
    greps = [turn(i, tool_chars=20_000) for i in range(4)]
    old = [{**t, "entries": [{k: v for k, v in e.items() if k != "t"} for e in t["entries"]]} for t in reads]
    for turns in (greps, old):
        clipped = evaluate_sync(turns, Candidate("clip", clip_chars=1000))
        assert clipped["tokens_after"] < clipped["tokens_before"] and clipped["changed_turns"] == 4


def test_memory_and_skill_candidates_cut_the_system_prompt_only():
    turns = [turn(i, memory=20_000, skills=[100] * 60) for i in range(5)]
    mem = evaluate_sync(turns, Candidate("memory", memory_chars=8000))
    skills = evaluate_sync(turns, Candidate("skills", skill_limit=20))
    assert mem["token_reduction_pct"] > 0 and mem["changed_turns"] == 5
    assert skills["token_reduction_pct"] > mem["token_reduction_pct"] / 2 and skills["changed_turns"] == 5
    untouched = evaluate_sync(turns, Candidate("none"))
    assert untouched["tokens_before"] == untouched["tokens_after"] and untouched["changed_turns"] == 0


def test_cheaper_tier_candidate_reports_a_token_delta_and_a_pass_delta():
    turns = [turn(i, tier="cheap", status="done" if i % 4 else "error", kind="title") for i in range(8)]
    turns += [turn(100 + i, tier="main", status="done", kind="title") for i in range(4)]
    delta = tier_pass_delta(turns, "title", "main")
    assert delta["token_delta_pct"] == 0.0
    assert delta["pass_delta_points"] == round(100 * (1.0 - 6 / 8), 2)  # main 100% vs cheap 6 of 8 = +25 points
    assert delta["observed_on_tier"] == 4 and delta["observed_elsewhere"] == 8
    assert tier_pass_delta(turns, "plan", "main")["pass_delta_points"] is None  # never observed: nothing to compare


def test_the_store_round_trips_skips_torn_lines_and_never_holds_text(tmp_path):
    store = ReplayStore(tmp_path)
    secret = "sk-abcdefgh12345678"
    assert store.append(turn(1, user=f"use key {secret} please"))
    with store.path.open("a", encoding="utf-8") as f:
        f.write('{"torn": \n')  # a crash mid-write
    loaded = store.load()
    assert len(loaded) == 1 and loaded[0]["turn"] == "t1" and loaded[0]["passed"] is True
    assert secret not in store.path.read_text(encoding="utf-8")  # sizes and outcomes only


def test_token_candidates_default_the_skills_limit_to_the_prompt_limit():
    """The default stayed a literal 60 after the prompt listing was cut to 30 (#85)."""
    turns = [turn(i, skills=[80] * (PROMPT_LIMIT + 5)) for i in range(5)]
    cands = asyncio.run(replay.token_candidates({}, turns))
    skills = next(c for c in cands if "skill_prompt_limit" in c["patch"]["context"])
    assert skills["title"] == f"List {SKILLS_CANDIDATE} skills in the prompt, not {PROMPT_LIMIT}"
    # a configured limit still wins over the default
    configured = asyncio.run(replay.token_candidates({"skill_prompt_limit": 25}, turns))
    assert next(c for c in configured if "skill_prompt_limit" in c["patch"]["context"])["title"].endswith("not 25")
    # a listing already at the candidate size has nothing to cut
    at_candidate = asyncio.run(replay.token_candidates({"skill_prompt_limit": SKILLS_CANDIDATE}, turns))
    assert all("skill_prompt_limit" not in c["patch"]["context"] for c in at_candidate)


def test_the_auto_gate_needs_a_real_reduction_and_a_bounded_pass_drop():
    assert auto_ok({"token_reduction_pct": 15.0, "pass_drop_points": 1.0, "changed_turns": 2})
    assert not auto_ok({"token_reduction_pct": 14.9, "pass_drop_points": 0.0, "changed_turns": 2})
    assert not auto_ok({"token_reduction_pct": 40.0, "pass_drop_points": 1.5, "changed_turns": 9})
    assert not auto_ok({"token_reduction_pct": 40.0, "pass_drop_points": 0.0, "changed_turns": 0})


def test_token_candidates_are_replayed_and_flagged_for_auto_only_when_the_gate_passes():
    # 300 small turns and 1 turn whose five tool results are large (today's clip keeps each at 10k): the clip to 6k
    # saves about a fifth of the tokens, and only 1 turn in 301 changes
    big = [turn(1000, tool_chars=10_000, tools=5)]
    turns = [turn(i) for i in range(300)] + big
    cands = asyncio.run(replay.token_candidates({}, turns))
    clip = next(c for c in cands if c["patch"] == {"context": {"tool_output_chars": 6000}})
    assert clip["evidence"]["token_reduction_pct"] >= 15 and clip["evidence"]["changed_turns"] == 1
    assert clip["auto_ok"] is True
    # the same clip where half the turns are large: the pass bound is too big, so it waits for a human
    half = [turn(i, tool_chars=60_000) for i in range(20)] + [turn(100 + i) for i in range(20)]
    half_cands = asyncio.run(replay.token_candidates({}, half))
    clip2 = next(c for c in half_cands if "tool_output_chars" in c["patch"]["context"])
    assert clip2["evidence"]["pass_drop_points"] == 50.0 and clip2["auto_ok"] is False
    # nothing to clip: no candidate at all
    none = asyncio.run(replay.token_candidates({}, [turn(i, tool_chars=100) for i in range(5)]))
    assert all("tool_output_chars" not in c["patch"]["context"] for c in none)


def _own_turn(n: int, tool_chars: int) -> list[Message]:
    return [
        Message(role="user", content=f"task {n}"),
        Message(role="assistant", content=None, tool_calls=[ToolCall(id=f"c{n}", name="grep", arguments={"p": n})]),
        Message(role="tool", content="x" * (tool_chars + n), tool_call_id=f"c{n}", name="grep"),
        Message(role="assistant", content=f"answer {n}"),
    ]


def _history(n: int, tool_chars: int = 12_000) -> list[Message]:
    """The system message and the ``n - 1`` turns before turn ``n`` of one session, as the gateway passes them."""
    msgs = [Message(role="system", content="system " + "s" * 100)]
    for i in range(1, n):
        msgs += _own_turn(i, tool_chars)
    return msgs


def _session_record(n: int):
    history = _history(n)
    return build_record(
        turn=f"t{n}", session="s1", kind="interactive_turn", tier="main", status="done",
        messages=history + _own_turn(n, 12_000), first=len(history), tokens_in=0, tokens_out=0,
        memory_chars=500, skill_lines=[80] * 5, ts=float(n),
    )  # fmt: skip


def _old_format(n: int):
    """The same turn as the old recorder wrote it: a size entry for every message of the whole history."""
    rec = _session_record(n)
    msgs = _history(n) + _own_turn(n, 12_000)
    old = {**rec, "entries": [replay._entry(m) for m in msgs]}
    old["boundaries"] = [i for i, m in enumerate(msgs) if m.role == "assistant" and i >= len(msgs) - 4]
    old.pop("history", None)
    return rec, old


def test_a_turn_record_does_not_grow_with_the_history_before_it():
    """Each record held a size entry for every earlier message: turn N wrote O(N), a session O(N^2) (issue #171)."""
    one, late = _session_record(1), _session_record(200)

    def size(r):
        return len(json.dumps(r, separators=(",", ":")))

    assert size(late) < 4 * size(one)
    assert len(late["entries"]) == len(one["entries"]) == 4  # only the turn's own messages


def test_the_summarised_history_replays_to_the_same_totals():
    """The history's characters and its largest tool results are kept, so the totals and the clip are unchanged."""
    for n in (1, 5, HISTORY_BIG_KEEP + 1):
        for clip in (1000, 12_020):
            rec, old = _old_format(n)
            cand = Candidate("clip", clip_chars=clip)
            assert evaluate_sync([rec], cand) == evaluate_sync([old], cand), (n, clip)


def test_a_long_history_never_makes_a_candidate_look_better_than_it_is():
    """Past the kept results the rest of the history is replayed unclipped: a smaller reduction, never a larger one."""
    rec, old = _old_format(40)
    cand = Candidate("clip", clip_chars=1000)
    new_result, old_result = evaluate_sync([rec], cand), evaluate_sync([old], cand)
    assert 0 < new_result["token_reduction_pct"] <= old_result["token_reduction_pct"]
    assert new_result["changed_turns"] == old_result["changed_turns"] and new_result["requests"] == 2


def test_a_record_in_the_old_format_still_replays():
    _, legacy = _old_format(3)
    result = evaluate_sync([legacy], Candidate("clip", clip_chars=1000))
    assert result["requests"] == 2 and result["tokens_after"] < result["tokens_before"]
