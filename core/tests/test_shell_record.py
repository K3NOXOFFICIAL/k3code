"""B1b: a `!cmd` with ``record`` becomes one user message of the transcript (command, output, a note), stored once,
never while a turn runs (the turn's persist would overwrite it), and every reader of "user messages" knows it."""

from __future__ import annotations

import asyncio
import json

from k3code.autonomy.advisor import transcript_text
from k3code.cli import _trim_history
from k3code.gateway.server import transcript_rows
from k3code.gateway.sessions import summarize_messages
from k3code.handoff import gather
from k3code.learning.review import transcript as review_transcript
from k3code.learning.review import user_turns
from k3code.providers.claude_cli import render_prompt
from k3code.providers.types import Message, messages_to_anthropic, messages_to_openai
from k3code.session_ai import (
    SHELL_RECORD_NOTE,
    is_shell_record,
    shell_record,
    task_anchor,
    work_ledger,
)
from k3code.tools.usershell import ShellRun
from test_autonomy_gateway import call, make, run_turn, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}
TOKEN = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"  # a GitHub token's shape, built so no scan sees a literal
#: a turn that takes a while: one bash call (sleep), then the answer
SLOW = [
    {"type": "tool_call", "name": "bash", "arguments": {"command": "sleep 1"}, "when": "turn_first"},
    {"type": "text", "text": "slept", "when": "turn_after_tool"},
]


def rec(command: str = "ls", out: str = "a.txt\n", code: int = 0) -> dict:
    return {"role": "user", "content": shell_record(command, ShellRun(out, "", code), 10_000)}


async def _server(tmp_path, monkeypatch, steps=None):
    server = make(tmp_path, monkeypatch, steps or [{"type": "text", "text": "ok"}], **NO_GATE)
    await start(server, tmp_path)
    return server


def _texts(server) -> list[str]:
    return [str(m.get("content") or "") for m in server.session.messages]


# ── the record ──


def test_the_record_has_the_command_the_output_and_the_note():
    text = shell_record("ls -la", ShellRun("a\nb\n", "warn\n", 2), 10_000)
    assert text.startswith("<bash-input>ls -la</bash-input>\n<bash-output>\na\nb\n[stderr]\nwarn\n[exit code 2]\n")
    assert text.endswith(f"</bash-output>\n{SHELL_RECORD_NOTE}")
    assert is_shell_record({"role": "user", "content": text})
    assert not is_shell_record({"role": "assistant", "content": text})
    assert not is_shell_record(Message(role="user", content="ls"))


def test_a_secret_in_the_command_or_the_output_is_masked():
    text = shell_record(f"echo {TOKEN}", ShellRun(f"token={TOKEN}\n", f"{TOKEN}\n", 0), 10_000)
    assert TOKEN not in text and TOKEN[:12] not in text


def test_a_huge_output_is_clipped_once_to_the_same_bytes():
    run = ShellRun("".join(f"line {i}\n" for i in range(150_000)), "", 0)  # ~1.4 MB
    first, second = shell_record("seq", run, 10_000), shell_record("seq", run, 10_000)
    assert first == second and len(first) < 12_000
    assert "line 0\n" in first and "line 149999" in first  # head and tail
    # stored as it is sent: the provider wire does not clip a user message again
    msgs = [Message(role="user", content=first), Message(role="user", content="next")]
    assert messages_to_openai(msgs)[0]["content"].startswith(first)


def test_a_long_command_is_cut():
    text = shell_record("x" * 5000, ShellRun("", "", 0), 10_000)
    assert "x" * 2000 + "…</bash-input>" in text and "x" * 2001 not in text


def test_cancelled_and_timed_out_commands_say_so():
    cancelled = shell_record("sleep 9", ShellRun("part\n", "", -15, cancelled=True), 10_000)
    assert "part\n[cancelled by the user]\n</bash-output>" in cancelled and "exit code" not in cancelled
    timed = shell_record("sleep 9", ShellRun("", "", -9, timed_out=True, error="timed out after 300 s (x)"), 10_000)
    assert "[timed out after 300 s (x)]" in timed and "exit code" not in timed


# ── recording through shell.exec ──


async def test_a_record_while_idle_goes_in_now_and_is_saved(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    res = await call(server, "shell.exec", {"command": "echo hi", "record": True})
    assert res["recorded"] == "now" and res["code"] == 0
    stored = server.store.get(server.session.session_id)
    assert is_shell_record(stored.messages[-1]) and "hi" in stored.messages[-1]["content"]
    assert (await call(server, "shell.exec", {"command": "echo x"}))["recorded"] is False  # record is opt-in


async def test_a_denied_command_records_nothing(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    res = await call(server, "shell.exec", {"command": "rm -rf /", "record": True})
    assert res["code"] == 126 and res["recorded"] is False
    assert not any(is_shell_record(m) for m in server.session.messages) and not server.session.shell_records


async def test_a_cancelled_command_is_recorded_as_cancelled(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    sid = server.session.session_id
    params = {"command": "echo part; sleep 30", "record": True, "exec_id": "c1"}
    frame = json.dumps({"jsonrpc": "2.0", "id": 11, "method": "shell.exec", "params": params})
    running = asyncio.ensure_future(server._handle_line(frame))
    for _ in range(200):
        if "c1" in server.session.user_shells:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    await call(server, "shell.cancel", {"session_id": sid, "exec_id": "c1"})
    await running
    res = next(json.loads(x)["result"] for x in server._frames if json.loads(x).get("id") == 11)
    assert res["code"] == 130 and res["recorded"] == "now"
    assert server.session.messages[-1]["content"].count("[cancelled by the user]") == 1


async def test_a_record_made_during_a_turn_lands_after_it_and_is_not_overwritten(tmp_path, monkeypatch):
    """THE hazard: the turn's persist rewrites the transcript from the loop's messages. The record waits and goes in
    when the turn ends (the done-callback: no further turn starts), after the turn's own messages."""
    server = await _server(tmp_path, monkeypatch, SLOW)
    await call(server, "prompt.submit", {"text": "first task"})
    task = server.session.turn_task
    await asyncio.sleep(0.3)
    assert server.session.turn_in_flight
    res = await call(server, "shell.exec", {"command": "echo during", "record": True})
    assert res["recorded"] == "after_turn"
    assert not any(is_shell_record(m) for m in server.session.messages)  # not while the turn runs
    await task
    await asyncio.sleep(0)  # the done-callback runs
    for messages in (server.session.messages, server.store.get(server.session.session_id).messages):
        texts = [str(m.get("content") or "") for m in messages]
        prompt = next(i for i, t in enumerate(texts) if t == "first task")
        answer = next(i for i, t in enumerate(texts) if t == "slept")
        record = next(i for i, m in enumerate(messages) if is_shell_record(m))
        assert prompt < answer < record and "during" in texts[record]
        assert any(m.get("role") == "tool" for m in messages)  # the turn's own steps are all there


async def test_a_prompt_drained_after_the_turn_sees_the_record_before_it(tmp_path, monkeypatch):
    """A prompt queued behind a turn runs in the same task (no done-callback yet): the loop top puts the record in
    first, so the model reads it, in order, before that prompt."""
    server = await _server(tmp_path, monkeypatch, SLOW)
    await call(server, "prompt.submit", {"text": "first task"})
    task = server.session.turn_task
    await asyncio.sleep(0.3)
    assert (await call(server, "shell.exec", {"command": "echo seen-by-next", "record": True}))[
        "recorded"
    ] == "after_turn"
    await call(server, "prompt.submit", {"text": "second task"})  # queued behind the running turn
    await task
    sent = [entry["text"] for entry in server.providers[0].log if "second task" in entry["text"]]
    assert sent and all(t.index("seen-by-next") < t.index("second task") for t in sent)
    assert sum(is_shell_record(m) for m in server.session.messages) == 1


async def test_the_next_prompt_after_an_idle_record_carries_it(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    await call(server, "shell.exec", {"command": "echo ctx-line", "record": True})
    await run_turn(server, "what did it print")
    text = server.providers[0].log[-1]["text"]
    assert text.index("ctx-line") < text.index("what did it print")


async def test_clear_drops_pending_records(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    server.session.shell_records.append(rec())
    await call(server, "slash.exec", {"command": "clear", "session_id": server.session.session_id})
    assert server.session.shell_records == [] and server.session.messages == []


async def test_undo_removes_the_prompt_and_keeps_the_record_and_retry_does_not_duplicate_it(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    await call(server, "shell.exec", {"command": "ls", "record": True})
    await run_turn(server, "explain")
    sid = server.session.session_id
    res = await call(server, "session.undo", {"session_id": sid})
    assert res["removed"] == 2  # the prompt and its answer
    assert is_shell_record(server.session.messages[-1])
    await run_turn(server, "explain")  # /retry: undo, then the same prompt again
    assert sum(is_shell_record(m) for m in server.session.messages) == 1
    assert _texts(server)[-2:] == ["explain", "ok"]


# ── readers of user messages ──


def test_the_task_anchor_skips_records():
    assert task_anchor([rec(), {"role": "user", "content": "the task"}])["content"] == "the task"


def test_the_ledger_lists_a_record_as_a_command_the_user_ran():
    ledger = work_ledger([rec(f"git log --oneline TOKEN={TOKEN} " + "x" * 200 + "\nsecond line")])
    line = next(x for x in ledger.splitlines() if x.startswith("ran: "))
    assert line.endswith("… (user)") and line.startswith("ran: git log --oneline") and TOKEN not in line


def test_the_advisor_transcript_labels_and_clips_a_record():
    big = rec("cat big", "y" * 9000)
    text = transcript_text([big, {"role": "user", "content": "q"}], per_message=500, per_role={"tool": 300})
    first = text.splitlines()[0]
    assert first.startswith("user ran (shell): <bash-input>cat big") and len(text) < 1000


def test_review_counts_no_record_as_a_turn_and_never_shows_its_output():
    messages = [rec("ls", "secret-ish output"), {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    assert user_turns(messages) == 1
    text = review_transcript(messages)
    assert "user ran: ls" in text and "secret-ish output" not in text


def test_the_preview_skips_records_and_falls_back_to_the_command():
    assert summarize_messages([rec("ls -la"), {"role": "user", "content": "real"}])[1] == "real"
    assert summarize_messages([rec("ls -la")])[1] == "!ls -la"


def test_transcript_rows_show_the_command_and_its_output():
    rows = transcript_rows([rec("make test", "boom\n", 2), {"role": "user", "content": "fix it"}])
    assert rows[0] == {"role": "user", "text": "!make test"}
    assert rows[1]["role"] == "system" and rows[1]["display_kind"] == "shell"
    assert rows[1]["text"] == "boom\n[exit code 2]"
    assert rows[1]["display_metadata"] == {"command": "make test", "code": 2}
    assert rows[2] == {"role": "user", "text": "fix it"}


def test_the_handoff_last_request_is_no_record(tmp_path):
    from k3code.gateway.sessions import StoredSession

    stored = StoredSession(session_id="s1", title="t", model="m", provider="p", cwd=str(tmp_path))
    stored.messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "b"},
        rec("ls"),
    ]
    h = gather(stored, cwd=tmp_path, usage_rows=[], now=0.0, hostname="h", git=None, memory={}, secrets=[])
    assert h.last_request == "second"
    assert not h.interrupted  # a record after the answer is no open request


def test_the_repl_trim_does_not_start_a_turn_at_a_record():
    history = [
        Message(role="user", content="one"),
        Message(role="assistant", content="a"),
        Message(role="assistant", content="b"),
        Message(role="user", content=rec()["content"]),
        Message(role="user", content="two"),
        Message(role="assistant", content="c"),
    ]
    assert [m.content for m in _trim_history(history)] == ["two", "c"]


# ── the wires: a record, then the prompt (two user messages in a row) ──


def _two_users() -> list[Message]:
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content=rec()["content"]),
        Message(role="user", content="q2"),
    ]


def test_openai_joins_a_record_and_the_prompt_into_one_user_turn():
    wire = messages_to_openai(_two_users())
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "user"]
    assert wire[-1]["content"].startswith("<bash-input>ls") and wire[-1]["content"].endswith("q2")


def test_anthropic_sends_both_user_messages_in_order_after_the_assistant():
    system, rest = messages_to_anthropic(_two_users())
    assert system == "sys" and [m["role"] for m in rest] == ["user", "assistant", "user", "user"]
    assert rest[2]["content"].startswith("<bash-input>") and rest[3]["content"] == "q2"


def test_claude_cli_renders_both_in_order():
    _system, prompt = render_prompt(_two_users(), [])
    assert prompt.index("[user]\n<bash-input>ls") < prompt.index("[user]\nq2")
