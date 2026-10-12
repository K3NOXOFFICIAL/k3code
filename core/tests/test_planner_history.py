"""The planner is sent the user's prompts and the assistant's words of older turns, and the last turns in full."""

from __future__ import annotations

from k3code.autonomy.plan_first import planner_history
from k3code.providers.types import Message, ToolCall


def turn(i: int) -> list[Message]:
    call = ToolCall(id=f"c{i}", name="read", arguments={"path": f"f{i}.py"})
    return [
        Message(role="user", content=f"request {i}"),
        Message(role="assistant", content=None, tool_calls=[call]),
        Message(role="tool", content="file text " * 500, tool_call_id=f"c{i}", name="read"),
        Message(role="assistant", content=f"answer {i}"),
    ]


def test_older_turns_shrink_to_prompts_and_answers_and_the_last_turns_stay_whole():
    history = [m for i in range(10) for m in turn(i)]
    out = planner_history(history, keep=8)
    assert [m.content for m in out if m.role == "user"][:10] == [f"request {i}" for i in range(10)]
    assert not any(m.role == "tool" for m in out[:-8]) and any(m.role == "tool" for m in out[-8:])
    assert out[-8:] == history[-8:]
    assert sum(len(m.content or "") for m in out) < sum(len(m.content or "") for m in history) // 3


def test_no_tool_call_is_left_without_its_result_or_the_other_way_round():
    history = [m for i in range(6) for m in turn(i)]
    out = planner_history(history, keep=6)
    calls = {c.id for m in out for c in m.tool_calls}
    results = {m.tool_call_id for m in out if m.role == "tool"}
    assert calls == results


def test_two_prompts_with_only_tool_work_between_them_become_one_user_turn():
    first = [
        Message(role="user", content="first ask"),
        Message(role="assistant", content=None, tool_calls=[ToolCall(id="a", name="read", arguments={})]),
        Message(role="tool", content="x", tool_call_id="a", name="read"),
        Message(role="user", content="second ask"),
    ]
    out = planner_history([*first, *[m for i in range(3) for m in turn(i)]], keep=4)
    roles = [m.role for m in out]
    assert all(a != b or a != "user" for a, b in zip(roles, roles[1:], strict=False)), "no two user turns in a row"
    assert out[0].content.startswith("first ask\n\nsecond ask")


def test_a_short_history_is_unchanged():
    history = turn(0) + turn(1)
    assert planner_history(history, keep=8) == history


def test_two_user_prompts_are_not_left_side_by_side_at_the_cut():
    from k3code.autonomy.plan_first import planner_history
    from k3code.providers.types import Message

    history = [
        Message(role="user", content="req 1"),
        Message(role="assistant", content=None),  # a turn that ended on a tool call: no answer in words
        Message(role="user", content="req 2"),
        Message(role="assistant", content="answer 2"),
        Message(role="user", content="req 3"),
        Message(role="assistant", content="answer 3"),
    ]
    out = planner_history(history, keep=3)
    roles = [m.role for m in out]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False)), roles
    assert out[0].role == "user" and "req 1" in out[0].content
