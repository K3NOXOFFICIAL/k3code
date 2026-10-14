"""`prompt.submit {target_session_id}`: the agent view prompts a live session the client is not attached to."""

from __future__ import annotations

import asyncio
import json

from test_autonomy_gateway import call, make, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}
STEPS = [
    {"type": "text", "match": "TARGETED", "text": "answer from the target"},
    {"type": "text", "match": "PLAIN", "text": "answer from the attached session"},
]


async def two_sessions(tmp_path, monkeypatch):
    """A client attached to session A while session B stays live and unattached."""
    server = make(tmp_path, monkeypatch, STEPS, **NO_GATE)
    await start(server, tmp_path)
    a = server.session
    await call(server, "session.create", {"cwd": str(tmp_path)})
    b = server.session
    assert b is not a
    await call(server, "session.activate", {"session_id": a.session_id})
    assert server.session is a and server.live.get(b.session_id) is b
    return server, a, b


async def error_of(server, params: dict) -> dict:
    n = len(server._frames)
    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "prompt.submit", "params": params}))
    return [json.loads(x) for x in server._frames[n:] if json.loads(x).get("id") == 9][0]["error"]


def contents(live) -> list[str]:
    return [str(m.get("content")) for m in live.messages]


async def test_prompt_runs_in_the_target_session_and_its_transcript(tmp_path, monkeypatch):
    server, a, b = await two_sessions(tmp_path, monkeypatch)
    params = {"session_id": a.session_id, "target_session_id": b.session_id, "text": "TARGETED go"}
    res = await call(server, "prompt.submit", params)
    assert res["status"] == "streaming"
    assert a.turn_task is None and b.turn_task is not None
    await b.turn_task
    await asyncio.sleep(0.05)
    assert contents(b)[-1] == "answer from the target"
    assert not any("TARGETED" in c or "answer from the target" in c for c in contents(a))
    assert server.session is a  # the client did not move


async def test_no_target_keeps_the_attached_session(tmp_path, monkeypatch):
    server, a, b = await two_sessions(tmp_path, monkeypatch)
    # `session_id` naming another session is still ignored: only `target_session_id` redirects.
    await call(server, "prompt.submit", {"session_id": b.session_id, "text": "PLAIN go"})
    assert b.turn_task is None and a.turn_task is not None
    await a.turn_task
    assert contents(a)[-1] == "answer from the attached session"


async def test_unknown_or_non_string_target_is_invalid_params(tmp_path, monkeypatch):
    server, a, _b = await two_sessions(tmp_path, monkeypatch)
    unknown = await error_of(server, {"target_session_id": "nope", "text": "TARGETED"})
    assert unknown["code"] == -32602 and "not running; attach it first" in unknown["message"]
    for bad in (5, ["x"], {"id": "x"}):
        err = await error_of(server, {"target_session_id": bad, "text": "TARGETED"})
        assert err["code"] == -32602 and "must be a string" in err["message"]
    assert a.turn_task is None
