"""RPCs the Ink TUI calls directly. Each used to answer "Method not found", which the TUI shows as "the terminal UI
and the k3code backend are out of sync": `!cmd`, /undo, /retry, /usage, /status, /save, /reload, /reload-mcp,
/reload-skills, pausing and steering sub-agents."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from k3code.gateway.protocol import TOO_MANY_REQUESTS
from k3code.gateway.server import _HANDLERS
from k3code.tools import jobs
from m1cmd_helpers import make_server
from test_autonomy_gateway import call, events, make, run_turn, start
from test_gateway_socket import Peer
from test_usershell import gone, kill_leftovers

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


async def test_shell_exec_needs_a_session_and_checks_its_params(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, REPLY, **NO_GATE)
    assert await _error(server, "shell.exec", {"command": "true"}) == "no active session: start or resume one first"
    await start(server, tmp_path)
    assert await _error(server, "shell.exec", {"command": "true", "session_id": "nope"}) == "unknown session: nope"
    for bad in ("has space", "x" * 65, "semi;colon"):
        msg = await _error(server, "shell.exec", {"command": "true", "exec_id": bad})
        assert msg.startswith("exec_id must be"), bad
    assert "longer than 32 KB" in await _error(server, "shell.exec", {"command": "x" * (32 * 1024 + 1)})
    res = await call(server, "shell.exec", {"command": "true", "exec_id": "A_b-9", "record": True})
    assert (res["code"], res["exec_id"], res["cwd"], res["recorded"]) == (0, "A_b-9", str(tmp_path), False)
    assert "cancelled" not in res and "timed_out" not in res
    assert (await call(server, "shell.exec", {"command": "true"}))["exec_id"]  # generated when absent


async def test_shell_exec_hardline_sees_the_session_directory(tmp_path, monkeypatch):
    """Relative rm targets ($PWD, ..) are only judged right with the cwd: the check must get it."""
    from k3code.permissions import hardline

    server = await _server(tmp_path, monkeypatch)
    marker = tmp_path / "ran"

    def spy(command, extra=None, *, cwd=None, home=None):
        return "needs-cwd" if cwd == str(tmp_path) else None

    monkeypatch.setattr(hardline, "check", spy)
    res = await call(server, "shell.exec", {"command": f"touch {marker}"})
    assert res["code"] == 126 and "needs-cwd" in res["stderr"] and not marker.exists()


async def test_shell_output_streams_as_events_when_asked(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    res = await call(server, "shell.exec", {"command": "echo one; echo two >&2", "exec_id": "s1", "stream": True})
    got = events(server, "shell.output")
    assert {(e["exec_id"], e["stream"], e["text"]) for e in got} == {
        ("s1", "stdout", "one\n"),
        ("s1", "stderr", "two\n"),
    }
    assert res["stdout"] == "one\n"
    await call(server, "shell.exec", {"command": "echo quiet"})
    assert len(events(server, "shell.output")) == 2  # no stream: no events


@pytest.fixture
async def daemon(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sock = tmp_path / "run" / "gw.sock"
    await server.start_socket(sock)
    server._running = True
    peer = await Peer.connect(sock)
    rid = await peer.send("session.create", {"cwd": str(tmp_path)})
    sid = (await peer.until(lambda f: f.get("id") == rid and "method" not in f))["result"]["session_id"]
    yield server, sock, peer, sid
    await server.stop_socket()
    await server.close()


async def _reply(peer: Peer, rid: int, timeout: float = 10.0) -> dict:
    return await peer.until(lambda f: f.get("id") == rid and "method" not in f, timeout)


async def test_shell_exec_does_not_hold_the_connection(daemon):
    """Awaited inline, a running `!cmd` kept every other frame of its client (session.interrupt, shell.cancel)
    unread until it ended."""
    _server, _sock, peer, sid = daemon
    slow = await peer.send("shell.exec", {"command": "sleep 1", "session_id": sid})
    started = time.monotonic()
    quick = await peer.send("process.list", {"session_id": sid})
    first = await peer.until(lambda f: f.get("id") in (slow, quick) and "method" not in f)
    assert first["id"] == quick and time.monotonic() - started < 0.3
    assert (await _reply(peer, slow))["result"]["code"] == 0


async def test_shell_cancel_stops_the_command_and_its_reply_still_arrives(daemon):
    server, _sock, peer, sid = daemon
    run = await peer.send("shell.exec", {"command": "sleep 30", "session_id": sid, "exec_id": "c1"})
    for _ in range(200):
        if "c1" in server.live[sid].user_shells:
            break
        await asyncio.sleep(0.02)
    pgid = jobs.REGISTRY.processes(sid)[0]["pid"]
    try:
        cancel = await peer.send("shell.cancel", {"session_id": sid, "exec_id": "c1"})
        replies = {}
        while len(replies) < 2:
            f = await peer.until(lambda f: f.get("id") in (run, cancel) and "method" not in f, 5)
            replies[f["id"]] = f["result"]
        assert replies[cancel] == {"cancelled": 1}
        assert replies[run]["code"] == 130 and replies[run]["cancelled"] is True
        assert server.live[sid].user_shells == {}
        assert await gone(pgid, 3)
    finally:
        kill_leftovers(pgid)


async def test_at_most_eight_commands_per_connection_and_cancel_all_of_a_session(daemon):
    server, _sock, peer, sid = daemon
    ids = [await peer.send("shell.exec", {"command": "sleep 30", "session_id": sid}) for _ in range(8)]
    extra = await peer.send("shell.exec", {"command": "true", "session_id": sid})
    refused = await _reply(peer, extra)
    assert refused["error"] == {
        "code": TOO_MANY_REQUESTS,
        "message": "too many concurrent requests on this connection (at most 8)",
    }
    for _ in range(200):
        if len(server.live[sid].user_shells) == 8:
            break
        await asyncio.sleep(0.02)
    cancel = await peer.send("shell.cancel", {"session_id": sid})
    replies: dict[int, dict] = {}
    while len(replies) < 9:
        f = await peer.until(lambda f: f.get("id") in (cancel, *ids) and "method" not in f, 5)
        replies[f["id"]] = f["result"]
    assert replies.pop(cancel) == {"cancelled": 8}
    assert [r["code"] for r in replies.values()] == [130] * 8


async def test_a_client_that_leaves_mid_command_takes_the_command_with_it(daemon):
    server, _sock, peer, sid = daemon
    await peer.send("shell.exec", {"command": "sleep 30 >/dev/null 2>&1 & sleep 31", "session_id": sid})
    for _ in range(200):
        if jobs.REGISTRY.processes(sid):
            break
        await asyncio.sleep(0.02)
    pgid = jobs.REGISTRY.processes(sid)[0]["pid"]
    try:
        peer.writer.close()
        assert await gone(pgid, 5)
    finally:
        kill_leftovers(pgid)


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


async def test_the_live_tail_and_the_caps_have_the_shape_the_tui_reads(tmp_path, monkeypatch):
    """The TUI's live view needs ``available: true`` (it said "unavailable" for every child, running or not), and
    the spawn-tree header shows the real concurrency cap (it showed ``d2/0``)."""
    from test_subagents import final, task_call

    steps = [
        task_call("CHILD-A find the answer"),
        final("parent done"),
        {"type": "text", "match": "[agent:worker]", "text": "the answer is 42"},
    ]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "PARENT: delegate it")
    (row,) = (await call(server, "subagent.list", {"session_id": server.session.session_id}))["subagents"]
    tail = await call(server, "subagent.tail", {"subagent_id": row["subagent_id"]})
    assert tail["available"] is True and tail["done"] is True and "the answer is 42" in tail["text"]
    assert tail["truncated"] is False
    assert (await call(server, "subagent.tail", {"subagent_id": "nope"}))["available"] is False
    caps = await call(server, "delegation.status", {})
    assert caps["max_concurrent_children"] == 3 and caps["max_spawn_depth"] == 2


async def test_the_command_catalog_lists_the_gateways_commands_for_help_and_alias_resolution(tmp_path, monkeypatch):
    """/help builds its list from this; without it only the TUI's own commands showed and /goal, /loop, /review...
    could not be discovered."""
    server = await _server(tmp_path, monkeypatch)
    cat = await call(server, "commands.catalog", {})
    names = {p[0] for p in cat["pairs"]}
    for command in ("/goal", "/loop", "/schedule", "/review", "/model", "/compact", "/mcp", "/skills", "/doctor"):
        assert command in names, command
    assert all(p[1] for p in cat["pairs"] if p[0] in ("/goal", "/loop")), "every row carries its help text"
    assert cat["canon"]["/compress"] == "/compact" and cat["canon"]["/m"] == "/model"
    assert cat["canon"]["/goal"] == "/goal"
    titles = [c["name"] for c in cat["categories"]]
    assert titles[0] == "Session" and "Autonomy" in titles
    listed = {p[0] for c in cat["categories"] for p in c["pairs"]}
    assert listed == names, "every command is in exactly one category"
    assert isinstance(cat["skill_count"], int)


async def test_focus_mode_from_the_config_reaches_the_tui(tmp_path, monkeypatch):
    server = await _server(tmp_path, monkeypatch)
    assert (await call(server, "config.get", {"key": "focus_view"}))["value"] == "0"
    server.config.display.focus_mode = True  # setup: "Focus mode on by default?" -> yes
    assert (await call(server, "config.get", {"key": "focus_view"}))["value"] == "1"


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
