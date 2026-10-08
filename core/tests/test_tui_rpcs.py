"""RPCs the Ink TUI calls directly. Each used to answer "Method not found", which the TUI shows as "the terminal UI
and the k3code backend are out of sync": `!cmd`, /undo, /retry, /usage, /status, /save, /reload, /reload-mcp,
/reload-skills, pausing and steering sub-agents."""

from __future__ import annotations

import json
from pathlib import Path

from k3code.gateway.server import _HANDLERS
from test_autonomy_gateway import call, make, run_turn, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}
REPLY = [{"type": "text", "text": "hello back"}]


async def _server(tmp_path, monkeypatch, steps=REPLY):
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    return server


async def _error(server, method: str, params: dict) -> str:
    n = len(server._frames)
    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": 9, "method": method, "params": params}))
    frame = next(json.loads(x) for x in server._frames[n:] if json.loads(x).get("id") == 9)
    return frame["error"]["message"]


def test_every_method_the_tui_calls_on_its_own_or_from_a_kept_command_exists():
    for method in (
        "shell.exec",
        "session.undo",
        "session.usage",
        "session.status",
        "session.save",
        "reload.mcp",
        "reload.env",
        "skills.reload",
        "delegation.status",
        "delegation.pause",
        "subagent.steer",
    ):
        assert method in _HANDLERS, method


async def test_shell_exec_runs_in_the_session_directory_and_keeps_the_hardline(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    res = await call(server, "shell.exec", {"command": "pwd; echo out; echo err >&2; exit 3"})
    assert res["code"] == 3
    assert res["stdout"].splitlines() == [str(Path(server.session.perms.cwd)), "out"]
    assert "err" in res["stderr"]
    denied = await call(server, "shell.exec", {"command": "rm -rf /"})
    assert denied["code"] == 126 and "hardline" in denied["stderr"]


async def test_undo_drops_the_last_exchange_and_retry_finds_nothing_left(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    await run_turn(server, "first question")
    await run_turn(server, "second question")
    before = len(server.session.messages)
    res = await call(server, "session.undo", {"session_id": server.session.session_id})
    assert res["removed"] >= 2
    assert len(server.session.messages) == before - res["removed"]
    users = [m["content"] for m in server.session.messages if m["role"] == "user"]
    assert users[-1] == "first question" and "second question" not in users
    stored = server.store.get(server.session.session_id)
    assert len(stored.messages) == len(server.session.messages)  # persisted, survives a restart
    await call(server, "session.undo", {})
    assert await call(server, "session.undo", {}) == {"removed": 0}


async def test_usage_status_and_save_describe_this_session(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    await run_turn(server, "hello")
    usage = await call(server, "session.usage", {"session_id": server.session.session_id})
    assert usage["calls"] >= 1 and usage["total"] == usage["input"] + usage["output"]
    status = await call(server, "session.status", {})
    assert "m-main" in status["output"] or "model" in status["output"].lower()
    saved = await call(server, "session.save", {})
    data = json.loads(Path(saved["file"]).read_text())
    assert Path(saved["file"]).parent == Path(server.session.perms.cwd)
    assert any(m.get("content") == "hello" for m in data["messages"])


async def test_reloads_answer_without_errors(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    assert (await call(server, "reload.mcp", {}))["status"] == "reloaded"
    assert isinstance((await call(server, "reload.env", {}))["updated"], int)
    assert isinstance((await call(server, "skills.reload", {}))["output"], str)


async def test_pausing_stops_new_sub_agents_and_steering_needs_a_running_one(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    assert (await call(server, "delegation.status", {}))["paused"] is False
    assert (await call(server, "delegation.pause", {"paused": True})) == {"paused": True}
    assert (await call(server, "delegation.status", {}))["paused"] is True
    try:
        server.subagents.spawn(server.session, description="x", prompt="y")
        raise AssertionError("spawn must refuse while paused")
    except RuntimeError as exc:
        assert "paused" in str(exc)
    assert (await call(server, "delegation.pause", {"paused": False})) == {"paused": False}
    steer = await call(server, "subagent.steer", {"subagent_id": "nope", "text": "go left"})
    assert steer["status"] == "not_queued"


async def test_undo_refuses_while_a_turn_runs(tmp_path, monkeypatch):
    import asyncio

    server = await _server(tmp_path, monkeypatch)
    server.session.turn_task = asyncio.get_running_loop().create_future()  # a turn that has not finished
    assert "turn is running" in await _error(server, "session.undo", {})
    server.session.turn_task.set_result(None)


async def test_compress_is_the_gateways_compact(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    res = await call(server, "slash.exec", {"command": "compress", "session_id": server.session.session_id})
    assert "Unknown command" not in json.dumps(res)
