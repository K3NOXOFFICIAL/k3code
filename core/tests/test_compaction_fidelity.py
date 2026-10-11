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
