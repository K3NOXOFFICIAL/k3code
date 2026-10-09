"""A system note in mid-conversation is sent as a user-side reminder: hoisting it rewrote the cached system prefix."""

from __future__ import annotations

from k3code.providers.types import Message, ToolCall, messages_to_anthropic


def call(i: int) -> Message:
    return Message(role="assistant", tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})])


def result(i: int) -> Message:
    return Message(role="tool", content=f"out {i}", tool_call_id=f"c{i}", name="bash")


def test_a_note_after_tool_results_joins_that_user_message_and_the_system_prompt_is_unchanged():
    base = [Message(role="system", content="SYSTEM PROMPT"), Message(role="user", content="go"), call(1), result(1)]
    note = Message(role="system", content="You are repeating the same call.")
    system_before, _ = messages_to_anthropic(base)
    system, rest = messages_to_anthropic([*base, note])
    assert system == system_before == "SYSTEM PROMPT"
    roles = [e["role"] for e in rest]
    assert roles == ["user", "assistant", "user"]  # alternation kept: no extra message
    blocks = rest[-1]["content"]
    assert blocks[0]["type"] == "tool_result"  # tool results stay first in their message
    assert blocks[1] == {
        "type": "text",
        "text": "<system-reminder>\nYou are repeating the same call.\n</system-reminder>",
    }


def test_the_note_keeps_its_place_in_the_next_request():
    base = [Message(role="system", content="S"), Message(role="user", content="go"), call(1), result(1)]
    note = Message(role="system", content="note")
    _, first = messages_to_anthropic([*base, note])
    _, second = messages_to_anthropic([*base, note, call(2), result(2)])
    assert second[: len(first)] == first  # the request after it starts with the same messages


def test_a_note_after_an_assistant_answer_goes_into_the_next_user_message():
    msgs = [
        Message(role="system", content="S"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="system", content="note"),
    ]
    _, rest = messages_to_anthropic(msgs)
    assert [e["role"] for e in rest] == ["user", "assistant", "user"]
    assert "<system-reminder>" in rest[-1]["content"][0]["text"]
    _, rest2 = messages_to_anthropic([*msgs, Message(role="user", content="next")])
    assert [e["role"] for e in rest2] == ["user", "assistant", "user"]
    assert "<system-reminder>" in rest2[-1]["content"][0]["text"]
    assert rest2[-1]["content"][1] == {"type": "text", "text": "next"}


def test_several_leading_system_messages_still_form_the_system_prompt():
    system, rest = messages_to_anthropic(
        [Message(role="system", content="A"), Message(role="system", content="B"), Message(role="user", content="q")]
    )
    assert system == "A\n\nB" and rest == [{"role": "user", "content": "q"}]
