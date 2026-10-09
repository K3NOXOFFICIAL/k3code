"""Session switching: needs-input state while an approval is open, session.close, transcript rows on resume."""

from __future__ import annotations

import pytest

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


def _worked_in(server, cwd: str) -> str:
    s = server.store.create(cwd=cwd)
    s.messages = [{"role": "user", "content": "hi"}]
    server.store.save(s)
    return s.session_id


async def test_most_recent_skips_sessions_with_no_message(tmp_path, monkeypatch):
    """With tui_auto_resume_recent a plain `k3code` resumes session.most_recent: an empty session left newest (a TUI
    start that never got a prompt) opened an empty chat instead of the user's last work."""
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    older = _worked_in(server, str(tmp_path))
    server.store.create(cwd=str(tmp_path))  # newer, empty
    assert server.store.most_recent().session_id == older
    assert (await call(server, "session.most_recent", {}))["session_id"] == older
    await server.close()


async def test_most_recent_on_a_store_of_only_empty_sessions_is_like_an_empty_store(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    assert await call(server, "session.most_recent", {}) == {"session_id": None}
    server.store.create(cwd=str(tmp_path))
    server.store.create(cwd=str(tmp_path))
    assert server.store.most_recent() is None
    assert await call(server, "session.most_recent", {}) == {"session_id": None}
    await server.close()


async def test_disposable_only_close_drops_an_empty_session_the_caller_left(tmp_path, monkeypatch):
    """The TUI sends session.close {disposable_only} for the session it just switched away from: an empty idle one
    goes, live and stored (nothing in it is lost, and it must not become the newest stored row)."""
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    res = await call(server, "session.close", {"session_id": a, "disposable_only": True})
    assert res == {"closed": False, "reason": "still in use"}  # the caller has not switched away yet
    assert a in server.live and server.store.get(a) is not None
    await call(server, "session.create", {"cwd": str(tmp_path)})  # the caller moves on
    assert (await call(server, "session.close", {"session_id": a, "disposable_only": True}))["closed"] is True
    assert a not in server.live and server.store.get(a) is None
    await server.close()


@pytest.mark.parametrize(
    ("busy", "reason"), [("message", "not disposable"), ("working", "still in use"), ("background", "still in use")]
)
async def test_disposable_only_close_leaves_a_session_with_content_or_work(tmp_path, monkeypatch, busy, reason):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    live = server.live[a]
    if busy == "message":
        live.stored.messages.append({"role": "user", "content": "hi"})
        server.store.save(live.stored)
    elif busy == "working":
        live.streaming = True
    else:
        live.background = True
    await call(server, "session.create", {"cwd": str(tmp_path)})  # the caller moves on
    res = await call(server, "session.close", {"session_id": a, "disposable_only": True})
    assert res == {"closed": False, "reason": reason}
    assert a in server.live and server.store.get(a) is not None
    live.streaming = False
    live.background = False
    await server.close()


async def test_default_close_keeps_the_stored_row_of_an_empty_session(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    await call(server, "session.create", {"cwd": str(tmp_path)})
    assert (await call(server, "session.close", {"session_id": a}))["closed"] is True
    assert a not in server.live and server.store.get(a) is not None
    await server.close()


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
