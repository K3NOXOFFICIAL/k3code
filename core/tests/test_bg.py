"""/bg and the Ctrl+B foreground→background handoff at the gateway level."""

from __future__ import annotations

import asyncio

from test_autonomy_gateway import call, events, make, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}


async def settle(server, live):
    await live.turn_task
    await asyncio.sleep(0.05)  # done-callbacks run on the next loop iteration


async def test_bg_with_prompt_starts_background_session_and_notifies(tmp_path, monkeypatch):
    steps = [{"type": "text", "match": "BGJOB", "text": "background answer"}]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    origin = server.session
    out = await call(server, "slash.exec", {"command": "bg BGJOB write the thing"})
    assert "Started background session" in out["output"]
    (bg,) = [s for s in server.live.values() if s.session_id != origin.session_id]
    assert bg.background and bg.stored.meta["origin_session"] == origin.session_id
    assert server.session is origin  # the client stays where it was
    await settle(server, bg)
    note = [n for n in events(server, "notification.show") if n.get("kind") == "background"]
    assert len(note) == 1 and "finished" in note[0]["text"] and "background answer" in note[0]["text"]
    assert bg.messages[-1]["content"] == "background answer"
    rows = (await call(server, "session.active_list"))["sessions"]
    assert {r["id"]: r["background"] for r in rows}[bg.session_id] is True
    assert events(server, "session.background_done")[0]["session_id"] == bg.session_id


async def test_bg_without_prompt_and_nothing_running(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], **NO_GATE)
    await start(server, tmp_path)
    out = await call(server, "slash.exec", {"command": "bg"})
    assert "Nothing is running" in out["output"]


async def test_prompt_background_hands_running_turn_off(tmp_path, monkeypatch):
    steps = [
        {
            "type": "tool_call",
            "match": "SLOWJOB",
            "when": "first",
            "name": "bash",
            "arguments": {"command": "sleep 0.4", "timeout": 5},
        },
        {"type": "text", "match": "SLOWJOB", "when": "after_tool", "text": "slow finished"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="yolo", **NO_GATE)
    await start(server, tmp_path)
    fg = server.session
    await call(server, "prompt.submit", {"text": "SLOWJOB please"})
    for _ in range(200):
        if fg.streaming:
            break
        await asyncio.sleep(0.01)
    assert fg.streaming
    res = await call(server, "prompt.background", {})
    assert res["status"] == "backgrounded" and res["session_id"] == fg.session_id
    fresh = server.live[res["new_session_id"]]
    assert server.session is fresh and fresh is not fg  # the client moved to a fresh, idle session
    assert fresh.perms.mode.value == "yolo" and fresh.stored.cwd == fg.stored.cwd and not fresh.background
    assert fg.background and fg.stored.meta["background"] is True
    assert fg.loop.background is True
    await settle(server, fg)
    assert fg.messages[-1]["content"] == "slow finished"
    note = [n for n in events(server, "notification.show") if n.get("kind") == "background"]
    assert len(note) == 1 and "finished" in note[0]["text"]


async def test_prompt_background_errors(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], **NO_GATE)
    await start(server, tmp_path)
    n = len(server._frames)
    await server._handle_line('{"jsonrpc":"2.0","id":9,"method":"prompt.background","params":{}}')
    import json

    err = [json.loads(x) for x in server._frames[n:] if json.loads(x).get("id") == 9][0]
    assert "nothing is running" in err["error"]["message"]


async def test_bg_refused_in_safe_mode(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], **NO_GATE)
    await start(server, tmp_path)
    server.background_paused = True
    out = await call(server, "slash.exec", {"command": "bg do it"})
    assert "paused" in out["output"]
