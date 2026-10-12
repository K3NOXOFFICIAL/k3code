"""A file read in an earlier turn and unchanged since is answered with a pointer, not sent again."""

from __future__ import annotations

from test_autonomy_gateway import call, make, run_turn

STEPS = [
    {"type": "tool_call", "id": "r", "name": "read", "arguments": {"path": "data.txt"}, "when": "turn_first"},
    {"type": "text", "text": "read it", "when": "turn_after_tool"},
]
AUTONOMY = {"plan_first": False, "proposals": False, "advisor_on_plan": False, "auto_continue": False}


def tool_results(server) -> list[str]:
    return [str(m["content"]) for m in server.session.stored.messages if m.get("role") == "tool"]


async def start(tmp_path, monkeypatch):
    (tmp_path / "data.txt").write_text("line one\nline two\n" * 40)
    server = make(tmp_path, monkeypatch, STEPS, mode="auto", autonomy=AUTONOMY)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    return server


async def test_the_second_turn_gets_a_pointer_to_the_first_turns_read(tmp_path, monkeypatch):
    server = await start(tmp_path, monkeypatch)
    await run_turn(server, "read data.txt")
    await run_turn(server, "read data.txt again")
    first, second = tool_results(server)[:2]
    assert "line one" in first and second.startswith("[unchanged since the read at step"), second


async def test_a_changed_file_is_read_again(tmp_path, monkeypatch):
    server = await start(tmp_path, monkeypatch)
    await run_turn(server, "read data.txt")
    (tmp_path / "data.txt").write_text("different\n" * 50)
    await run_turn(server, "read data.txt again")
    assert "different" in tool_results(server)[1]


async def test_after_a_clear_the_file_is_read_in_full_again(tmp_path, monkeypatch):
    server = await start(tmp_path, monkeypatch)
    await run_turn(server, "read data.txt")
    await call(server, "slash.exec", {"command": "clear"})
    await run_turn(server, "read data.txt once more")
    assert "line one" in tool_results(server)[-1], "no pointer to a result the clear removed"


async def test_after_an_undo_the_file_is_read_in_full_again(tmp_path, monkeypatch):
    """/undo rewinds the history without clearing the read bookkeeping: no pointer to a removed result may go out."""
    server = await start(tmp_path, monkeypatch)
    await run_turn(server, "read data.txt")
    await run_turn(server, "read data.txt again")
    assert tool_results(server)[1].startswith("[unchanged since the read")
    await call(server, "session.undo", {})
    await call(server, "session.undo", {})  # both turns are gone: the first result is no longer in the conversation
    assert tool_results(server) == []
    await run_turn(server, "read data.txt a third time")
    assert "line one" in tool_results(server)[-1], "the model has never seen the content in this conversation"
