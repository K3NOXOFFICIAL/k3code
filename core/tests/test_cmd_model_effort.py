"""Gateway /model and /effort act on the session they are typed in."""

from __future__ import annotations

from test_permissions_gateway import call, make_server


async def test_model_command_switches_the_sessions_model_and_rejects_unknown_keys(tmp_path, monkeypatch):
    """/model <key> only changed config.default_model (a session with its own stored.model never read it) and
    accepted any typo."""
    server, _ = make_server(tmp_path, [], monkeypatch)
    server.config.providers[0].models = {"default": "m", "cheap": "c"}
    sid = (await call(server, "session.create", {"cwd": str(tmp_path), "model": "default"}))["session_id"]
    out = await call(server, "command.dispatch", {"name": "model", "arg": "cheap", "session_id": sid})
    assert out["message"] == "Model key set to: cheap"
    live = server._session_for(sid)
    assert live.stored.model == "cheap" and server.store.get(sid).model == "cheap"
    assert server.config.default_model == "default"
    out = await call(server, "command.dispatch", {"name": "model", "arg": "cheapp", "session_id": sid})
    assert "Unknown model key: cheapp" in out["message"] and live.stored.model == "cheap"
    out = await call(server, "command.dispatch", {"name": "model", "arg": "", "session_id": sid})
    assert out["message"] == "Current model key: cheap"


async def test_effort_is_stored_on_the_session_and_reported_in_session_info(tmp_path, monkeypatch):
    """/effort validated, replied "set" and stored nothing."""
    server, _ = make_server(tmp_path, [], monkeypatch)
    sid = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    out = await call(server, "command.dispatch", {"name": "effort", "arg": "high", "session_id": sid})
    assert out["message"] == "Reasoning effort set to: high"
    live = server._session_for(sid)
    assert live.reasoning_effort == "high" and live.live_info()["reasoning_effort"] == "high"
    server.live.pop(sid, None)
    assert server.live_for(server.store.get(sid)).reasoning_effort == "high"  # survives a resume
    out = await call(server, "command.dispatch", {"name": "effort", "arg": "max", "session_id": sid})
    assert "Unknown effort" in out["message"]
