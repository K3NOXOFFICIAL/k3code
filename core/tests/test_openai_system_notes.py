"""A system note in mid-conversation is sent as a user-side reminder on the chat-completions wire too: strict chat
templates (Mistral, Gemma) answer HTTP 400 to a system message that is not first, and a relay that hoists it into a
Claude system prompt rewrites the cached prefix."""

from __future__ import annotations

from k3code.providers.types import Message, ToolCall, messages_to_openai


def call(i: int) -> Message:
    return Message(role="assistant", tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})])


def result(i: int) -> Message:
    return Message(role="tool", content=f"out {i}", tool_call_id=f"c{i}", name="bash")


def reminder(text: str) -> str:
    return f"<system-reminder>\n{text}\n</system-reminder>"


def roles(wire: list[dict]) -> list[str]:
    return [e["role"] for e in wire]


def test_a_note_after_tool_results_is_a_user_message_and_no_system_message_follows_the_first():
    base = [Message(role="system", content="SYSTEM PROMPT"), Message(role="user", content="go"), call(1), result(1)]
    before = messages_to_openai(base)
    wire = messages_to_openai([*base, Message(role="system", content="You are repeating the same call.")])
    assert roles(wire) == ["system", "user", "assistant", "tool", "user"]
    assert wire[0] == before[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert wire[:-1] == before  # everything before the note is unchanged
    assert wire[-1] == {"role": "user", "content": reminder("You are repeating the same call.")}


def test_the_note_keeps_its_place_in_the_next_request():
    base = [Message(role="system", content="S"), Message(role="user", content="go"), call(1), result(1)]
    note = Message(role="system", content="note")
    first = messages_to_openai([*base, note])
    second = messages_to_openai([*base, note, call(2), result(2)])
    assert second[: len(first)] == first


def test_a_note_after_an_assistant_answer_joins_the_next_user_message():
    msgs = [
        Message(role="system", content="S"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="system", content="note"),
    ]
    assert messages_to_openai(msgs)[-1] == {"role": "user", "content": reminder("note")}
    wire = messages_to_openai([*msgs, Message(role="user", content="next")])
    assert roles(wire) == ["system", "user", "assistant", "user"]  # user and assistant turns still alternate
    assert wire[-1] == {"role": "user", "content": reminder("note") + "\n\nnext"}


def test_notes_in_a_row_are_one_user_message_and_an_empty_note_is_dropped():
    wire = messages_to_openai(
        [
            Message(role="system", content="S"),
            Message(role="user", content="go"),
            call(1),
            result(1),
            Message(role="system", content="first"),
            Message(role="system", content=""),
            Message(role="system", content="second"),
        ]
    )
    assert roles(wire) == ["system", "user", "assistant", "tool", "user"]
    assert wire[-1]["content"] == reminder("first") + "\n\n" + reminder("second")


def test_several_leading_system_messages_stay_system_messages():
    wire = messages_to_openai(
        [Message(role="system", content="A"), Message(role="system", content="B"), Message(role="user", content="q")]
    )
    assert wire == [
        {"role": "system", "content": "A"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "q"},
    ]
