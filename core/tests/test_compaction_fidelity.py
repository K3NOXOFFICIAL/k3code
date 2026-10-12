"""What survives a compaction: the first task statement (head and tail), what the summary model reads, the todo list."""

from __future__ import annotations

from k3code.session_ai import (
    COMPACT_TOOL_CHARS,
    SUMMARY_PREFIX,
    TASK_ANCHOR_CHARS,
    compact_messages,
)
from learn_helpers import FakeCaller
from m1cmd_helpers import make_server, new_session


def filler(n: int, *, tool: str = "") -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"request {i} " + "x" * 300})
        out.append({"role": "assistant", "content": f"answer {i} " + "y" * 300})
        if tool:
            out.append({"role": "tool", "name": "bash", "tool_call_id": f"t{i}", "content": tool})
    return out


async def test_a_requirement_at_the_end_of_a_long_first_prompt_survives_every_compaction():
    instruction = "FINAL REQUIREMENT: keep the public API unchanged"
    first = "context dump " + "z" * 40_000 + " " + instruction
    msgs = [{"role": "user", "content": first}, *filler(12)]
    caller = FakeCaller("the work so far")
    new, folded = await compact_messages(caller, msgs, keep=4)
    assert folded > 0
    anchor = new[0]["content"]
    assert instruction in anchor and anchor.startswith("context dump") and len(anchor) <= TASK_ANCHOR_CHARS
    assert "chars omitted" in anchor
    # twelve more rounds: the anchor is never re-clipped (so its omitted count stays true) and the requirement stays
    for _ in range(12):
        new, _ = await compact_messages(caller, [*new, *filler(8)], keep=4)
    assert new[0]["content"] == anchor


async def test_the_summary_model_reads_the_end_of_tool_results_and_long_user_messages():
    plan = "PLAN: " + "step detail " * 700 + "LAST STEP: publish the release notes"  # ~8.4k chars, a later message
    result = "pytest output " + "." * 3000 + "== 3 failed, 41 passed =="
    msgs = [{"role": "user", "content": "first task"}, *filler(3, tool=result), {"role": "user", "content": plan}]
    msgs += filler(6, tool=result)
    caller = FakeCaller("summary")
    await compact_messages(caller, msgs, keep=4)
    seen = caller.calls[0][1].content
    assert "== 3 failed, 41 passed ==" in seen, "a tool result keeps its end, where the verdict is"
    assert "LAST STEP: publish the release notes" in seen, "a later plan is not cut at 3000 characters"
    # and a tool result is not shown whole
    assert max(len(line) for line in seen.splitlines() if line.startswith("tool bash")) < COMPACT_TOOL_CHARS + 40


async def test_an_earlier_summary_is_shown_whole_to_the_next_one():
    old_summary = SUMMARY_PREFIX + "DECISIONS: " + "d" * 3500 + " NEXT STEP: run the migration"
    msgs = [{"role": "user", "content": "task"}, {"role": "user", "content": old_summary}, *filler(8)]
    caller = FakeCaller("summary")
    await compact_messages(caller, msgs, keep=4)
    assert "NEXT STEP: run the migration" in caller.calls[0][1].content


async def test_a_long_first_prompt_does_not_push_the_earlier_summary_out_of_the_next_summarizers_input():
    first = "context dump " + "z" * 40_000 + " FINAL REQUIREMENT: keep the public API unchanged"
    msgs = [{"role": "user", "content": first}, *filler(12)]
    new, folded = await compact_messages(FakeCaller("DECISION-ALPHA: use the sqlite backend"), msgs, keep=4)
    assert folded > 0 and len(new[0]["content"]) > 8_000  # the anchor alone is bigger than the summarizer's head

    second = FakeCaller("DECISION-ALPHA and DECISION-BETA")
    await compact_messages(second, [*new, *filler(100, tool="r" * 900)], keep=4)
    seen = second.calls[0][1].content
    assert "DECISION-ALPHA: use the sqlite backend" in seen, "the first summary reaches the second one"
    assert "FINAL REQUIREMENT" in seen, "and the gist of the task is still shown, the rest is kept verbatim"
    assert len(seen) < 60_000 + 200


# ── the todo list ─────────────────────────────────────────────────────────────────────────────────────────────────

TODOS = [
    {"content": "write the parser", "status": "completed"},
    {"content": "wire it into the CLI", "status": "in_progress"},
    {"content": "document it", "status": "pending"},
]


def todo_call(i: int) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": f"todo{i}", "name": "todo", "arguments": {"todos": TODOS}}],
    }


async def seeded(tmp_path, monkeypatch, messages: list[dict]):
    server, _ = make_server(tmp_path, monkeypatch, ["a summary of the work"], context={"keep_messages": 4})
    live = server.live[await new_session(server, tmp_path)]
    live.stored.messages = messages
    live.stored.meta["todos"] = TODOS
    return server, live


async def test_the_todo_list_is_carried_into_the_summary_when_its_call_is_folded(tmp_path, monkeypatch):
    msgs = [{"role": "user", "content": "build it"}, todo_call(1), *filler(10)]
    server, live = await seeded(tmp_path, monkeypatch, msgs)
    assert await server.compact_session(live) > 0
    summary = next(m["content"] for m in live.stored.messages if str(m["content"]).startswith(SUMMARY_PREFIX))
    assert "Todos (1/3 done):" in summary and "wire it into the CLI" in summary
    # a second compaction keeps it: the folded summary carried the list
    live.stored.messages = [*live.stored.messages, *filler(10)]
    assert await server.compact_session(live) > 0
    summary = next(
        m["content"]
        for m in live.stored.messages
        if str(m["content"]).startswith(SUMMARY_PREFIX) and "Todos" in m["content"]
    )
    assert "document it" in summary
    await server.close()


async def test_the_list_is_not_added_when_nothing_folded_wrote_it(tmp_path, monkeypatch):
    # the list in the meta is left over from before a /clear: nothing in this conversation made it
    server, live = await seeded(tmp_path, monkeypatch, [{"role": "user", "content": "new task"}, *filler(10)])
    assert await server.compact_session(live) > 0
    assert not any("Todos (" in str(m["content"]) for m in live.stored.messages)
    await server.close()


async def test_the_list_is_not_added_when_the_kept_tail_has_its_own_todo_call(tmp_path, monkeypatch):
    msgs = [
        {"role": "user", "content": "build it"},
        todo_call(1),
        *filler(10),
        todo_call(2),
        {"role": "tool", "content": "ok"},
    ]
    server, live = await seeded(tmp_path, monkeypatch, msgs)
    assert await server.compact_session(live) > 0
    assert not any("Todos (" in str(m["content"]) for m in live.stored.messages if m["role"] == "user")
    await server.close()


async def test_a_cleared_list_is_not_injected(tmp_path, monkeypatch):
    server, live = await seeded(tmp_path, monkeypatch, [{"role": "user", "content": "x"}, todo_call(1), *filler(10)])
    live.stored.meta["todos"] = []
    assert await server.compact_session(live) > 0
    assert not any("Todo list cleared" in str(m["content"]) for m in live.stored.messages)
    await server.close()


# ── the goal reviewer's summary of a growing conversation ────────────────────────────────────────────────────────


async def test_a_second_review_summarizes_only_what_is_new_since_the_first():
    from k3code.autonomy.advisor import condensed_context

    def convo(n: int) -> list[dict]:
        return [
            m
            for i in range(n)
            for m in (
                {"role": "user", "content": f"ask {i} " + "q" * 300},
                {"role": "assistant", "content": f"did {i} " + "a" * 300},
            )
        ]

    caller = FakeCaller("SUMMARY ONE", "SUMMARY TWO")
    memo: dict = {}
    first = await condensed_context(caller, convo(10), threshold=2000, memo=memo)
    assert "SUMMARY ONE" in first and len(caller.calls) == 1
    assert "ask 5" in caller.calls[0][1].content  # the whole old part, as far as the size limit reaches

    second = await condensed_context(caller, convo(14), threshold=2000, memo=memo)
    assert "SUMMARY TWO" in second and len(caller.calls) == 2
    sent_second = caller.calls[1][1].content
    assert "[summary so far]\nSUMMARY ONE" in sent_second, "the earlier summary stands in for the covered messages"
    assert "ask 5 " not in sent_second and "ask 8 " in sent_second  # only the messages after the covered ones

    # a conversation that does not start the same way is summarized from its own start
    other = [{"role": "user", "content": "something else entirely " + "z" * 400}] + convo(14)[1:]
    await condensed_context(FakeCaller("S"), other, threshold=2000, memo=memo)
    # short messages, so the "[summary so far]" prefix survives the cut to the last 4000 characters: only the signature
    # check can keep a memo of another conversation out
    short = [{"role": r, "content": f"{r} {i}"} for i in range(12) for r in ("user", "assistant")]
    stale = FakeCaller("S3")
    await condensed_context(stale, short, threshold=300, memo={"n": 4, "sig": "stale", "summary": "OLDSUMMARY"})
    assert "OLDSUMMARY" not in stale.calls[0][1].content
    fresh_memo: dict = {}
    first = FakeCaller("COVERED")
    await condensed_context(first, short[:20], threshold=300, memo=fresh_memo)
    again = FakeCaller("S4")
    await condensed_context(again, short, threshold=300, memo=fresh_memo)
    assert "[summary so far]\nCOVERED" in again.calls[0][1].content, "a memo of this conversation is used"

    # no memo: as before
    caller4 = FakeCaller("S4")
    await condensed_context(caller4, convo(10), threshold=2000)
    assert "[summary so far]" not in caller4.calls[0][1].content


async def test_an_empty_summary_is_not_remembered_as_covering_the_messages_it_failed_to_summarize():
    from k3code.autonomy.advisor import condensed_context

    convo = [
        m
        for i in range(14)
        for m in (
            {"role": "user", "content": f"ask {i} " + "q" * 300},
            {"role": "assistant", "content": f"did {i} " + "a" * 300},
        )
    ]
    caller = FakeCaller("", "SUMMARY")  # a reasoning model that spent its budget without writing
    memo: dict = {}
    text = await condensed_context(caller, convo, threshold=2000, memo=memo)
    assert memo == {}, "nothing was summarized, so nothing is covered"
    assert "ask 13" in text, "the review still gets a hard-truncated transcript"
    await condensed_context(caller, convo, threshold=2000, memo=memo)
    assert "[summary so far]" not in caller.calls[1][1].content, "the second call summarizes from the start"
    assert memo["summary"] == "SUMMARY"


async def test_a_read_pointer_whose_result_was_folded_is_reworded_and_one_that_is_still_there_is_kept():
    from k3code.context_budget import POINTER_CALL_RE, read_pointer

    assert POINTER_CALL_RE.match(read_pointer(3, "call_9")).group(1) == "call_9"

    def step(i: int, content: str) -> list[dict]:
        return [
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"r{i}", "name": "read", "arguments": {}}]},
            {"role": "tool", "name": "read", "tool_call_id": f"r{i}", "content": content},
        ]

    msgs = [
        {"role": "user", "content": "first task"},
        *step(1, "     1\tSECRET_MARKER " + "x" * 600),
        *filler(10),
        {"role": "user", "content": "look again"},
        *step(2, read_pointer(1, "r1")),  # points at the folded read
        *step(3, "     1\tfresh read " + "y" * 600),
        *step(4, read_pointer(2, "r3")),  # points at a result that is still in the history
        {"role": "assistant", "content": "done"},
    ]
    new, folded = await compact_messages(FakeCaller("summary"), msgs, keep=7)
    assert folded > 0
    kept = {m["tool_call_id"]: m["content"] for m in new if m.get("role") == "tool"}
    assert "r1" not in kept and "above still holds" not in kept["r2"] and "summary above" in kept["r2"]
    assert kept["r4"].startswith("[unchanged since the read at step 2 (call r3)")
