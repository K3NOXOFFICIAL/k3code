"""Titles and /compact run on the `title` / `compaction` task kinds through ModelCaller."""

from __future__ import annotations

import asyncio

from k3code.session_ai import clean_title, split_for_compaction
from m1cmd_helpers import cmd, frames_of, make_server, new_session, submit_and_wait


def kinds(server):
    return server.usage._db.execute("SELECT task_kind, tier FROM events WHERE kind='call'").fetchall()


def test_clean_title_and_split():
    assert clean_title('"Fix the login bug."\nextra') == "Fix the login bug"
    msgs = [
        {"role": "user"},
        {"role": "assistant"},
        {"role": "user"},
        {"role": "assistant", "tool_calls": []},
        {"role": "tool"},
        {"role": "assistant"},
    ]
    assert split_for_compaction(msgs, keep=2) == 2  # never cuts between a tool call and its result


async def test_auto_title_uses_title_kind_when_enabled(tmp_path, monkeypatch):
    server, provider = make_server(
        tmp_path,
        monkeypatch,
        replies=["done", '"Refactor the parser."'],
        autonomy={"auto_title": True, "advisor_on_goal": False},
    )
    sid = await new_session(server, tmp_path)
    await submit_and_wait(server, "please refactor the parser")
    await asyncio.gather(*list(server._side_tasks))
    assert server.store.get(sid).title == "Refactor the parser"
    assert ("title", "cheap") in kinds(server)
    assert any(f.get("params", {}).get("type") == "session.title" for f in frames_of(server))
    await server.close()


async def test_no_auto_title_by_default(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, replies=["done"])
    sid = await new_session(server, tmp_path)
    await submit_and_wait(server, "hello")
    assert provider.n == 1 and not server.store.get(sid).title
    await server.close()


async def test_compact_summarizes_old_messages_via_compaction_kind(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, replies=["SUMMARY: did a and b"])
    sid = await new_session(server, tmp_path)
    live = server.sessions.get(sid)
    live.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"} for i in range(12)]
    res = await cmd(server, "/compact", sid)
    assert "Compacted" in res["message"]
    msgs = server.sessions.get(sid).messages
    assert msgs[0]["content"] == "message 0"  # the task statement is kept verbatim ahead of the summary
    assert msgs[1]["content"].startswith("[summary of earlier conversation]\nSUMMARY: did a and b")
    assert msgs[-1]["content"] == "message 11" and len(msgs) < 12
    assert ("compaction", "cheap") in kinds(server)
    await server.close()


async def test_compact_small_transcript_is_a_noop(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    server.sessions.get(sid).messages = [{"role": "user", "content": "hi"}]
    assert "Nothing to compact" in (await cmd(server, "/compact", sid))["message"]
    assert provider.n == 0
    await server.close()


def _long_transcript(task: str, filler: int = 30) -> list[dict]:
    msgs = [{"role": "system", "content": "system prompt"}, {"role": "user", "content": task}]
    for i in range(filler):
        msgs.append({"role": "assistant", "content": f"step {i} " + "x" * 3000})
        msgs.append({"role": "user", "content": f"continue {i} " + "y" * 3000})
    return msgs


async def test_compaction_keeps_the_task_statement_and_the_head_and_tail(tmp_path):
    from k3code.session_ai import SUMMARY_PREFIX, compact_messages, split_for_compaction
    from learn_helpers import FakeCaller

    task = "TASK: migrate the parser to v2 and keep every test green"
    msgs = _long_transcript(task)
    caller = FakeCaller("the parser is half migrated")
    new, folded = await compact_messages(caller, msgs, keep=6)
    assert folded > 0 and any(m["content"] == task for m in new)  # verbatim, ahead of the summary
    assert any(str(m["content"]).startswith(SUMMARY_PREFIX) for m in new)
    sent = caller.calls[0][1].content
    last_folded = msgs[split_for_compaction(msgs, 6) - 1]["content"]
    assert task in sent and last_folded[:12] in sent  # the head (the task) and the tail reach the summary model
    assert "middle of the conversation omitted" in sent and len(sent) < 70_000
    # a second compaction still keeps the task statement (the anchor is found past earlier summaries)
    again, _ = await compact_messages(caller, new + _long_transcript("later", filler=20)[2:], keep=6)
    assert any(m["content"] == task for m in again)


def test_unknown_escalate_key_is_warned_about(tmp_path, monkeypatch, caplog):
    import logging

    from k3code.config import load_config
    from k3code.paths import user_config_path

    text = "autonomy:\n  escalate:\n    judge_not_done: 2\n    tool_errors: 4\n"
    user_config_path().write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="k3code.config"):
        cfg = load_config(project_dir=tmp_path)
    assert "autonomy.escalate.judge_not_done is not used" in caplog.text
    assert "autonomy.escalate.tool_errors" not in caplog.text and cfg.autonomy["escalate"]["tool_errors"] == 4
