"""Session switching: needs-input state while an approval is open, session.close, transcript rows on resume."""

from __future__ import annotations

from k3code.gateway.server import transcript_rows
from test_permissions_gateway import call, make_server


async def test_open_approval_makes_the_session_need_input(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sid = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    live = server.live[sid]
    assert live.state == "idle"
    server._open_requests["approval-1"] = (sid, "{}")
    assert live.state == "needs_input" and server.has_open_request(sid)
    row = next(r for r in (await call(server, "session.active_list", {}))["sessions"] if r["id"] == sid)
    assert row["state"] == "needs_input" and row["status"] == "needs_input"
    server._open_requests.clear()
    assert live.state == "idle"


async def test_session_close_drops_idle_sessions_but_keeps_busy_ones(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    b = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]  # the client moves to b
    server._open_requests["approval-1"] = (a, "{}")
    assert (await call(server, "session.close", {"session_id": a}))["closed"] is False  # waiting for an answer
    server._open_requests.clear()
    assert (await call(server, "session.close", {"session_id": a}))["closed"] is True
    assert a not in server.live and server.store.get(a) is not None  # still resumable
    assert (await call(server, "session.close", {"session_id": b}))["closed"] is False  # the client is on it
    assert (await call(server, "session.close", {"session_id": "nope"}))["closed"] is False


def test_transcript_rows_shape():
    rows = transcript_rows(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "bash", "arguments": "ls"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert rows == [
        {"role": "user", "text": "hi"},
        {"role": "tool", "name": "bash", "context": "ls"},
        {"role": "assistant", "text": "done"},
    ]
