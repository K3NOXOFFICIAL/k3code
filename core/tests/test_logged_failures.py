"""Housekeeping failures are logged, not swallowed; a NetWatch that fails to start marks its session.

`contextlib.suppress(Exception)` around NetWatch's start hid the failure: offline pause/resume was silently off."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from k3code import daemon, doctor
from k3code.reliability.hooks import Reliability
from test_autonomy_gateway import call, events, make, run_turn, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}
REPLY = [{"type": "text", "text": "hello back"}]


async def _boom(self) -> None:
    raise RuntimeError("nmcli is gone")


async def test_a_netwatch_that_fails_to_start_is_logged_and_marks_the_session(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(Reliability, "start", _boom)
    server = make(tmp_path, monkeypatch, REPLY, **NO_GATE)
    await start(server, tmp_path)
    with caplog.at_level(logging.ERROR, logger="k3code.gateway"):
        await run_turn(server, "hi")
    assert server.session.stored.messages[-1]["content"] == "hello back"  # the turn still ran
    assert any("NetWatch did not start" in r.getMessage() and r.exc_info for r in caplog.records)
    assert server.session.offline_protection_error == "nmcli is gone"
    notes = [n for n in events(server, "notification.show") if n.get("key") == server.NETWATCH_KEY]
    assert len(notes) == 1 and "offline protection off" in notes[0]["text"]
    rows = (await call(server, "session.active_list", {"current_session_id": server.session.session_id}))["sessions"]
    assert next(r for r in rows if r["current"])["offline_protection"] is False


async def test_doctor_reports_a_session_whose_offline_protection_is_off(tmp_path, monkeypatch):
    async def no_checks(config):
        return []

    monkeypatch.setattr(doctor, "run_checks", no_checks)  # the global probes are not under test (and hit the network)
    monkeypatch.setattr(Reliability, "start", _boom)
    server = make(tmp_path, monkeypatch, REPLY, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "hi")
    out = (await call(server, "slash.exec", {"command": "doctor", "session_id": server.session.session_id}))["output"]
    assert "offline protection: off for this session" in out and "nmcli is gone" in out


async def test_doctor_is_silent_about_offline_protection_when_netwatch_runs(tmp_path, monkeypatch):
    async def no_checks(config):
        return []

    monkeypatch.setattr(doctor, "run_checks", no_checks)
    server = make(tmp_path, monkeypatch, REPLY, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "hi")
    out = (await call(server, "slash.exec", {"command": "doctor", "session_id": server.session.session_id}))["output"]
    assert "offline protection" not in out


async def test_a_failed_idle_netwatch_stop_is_logged(tmp_path, monkeypatch, caplog):
    server = make(tmp_path, monkeypatch, REPLY, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "hi")
    live = server.session
    assert live.reliability is not None and live.reliability._started
    assert next(r for r in (await call(server, "session.active_list", {}))["sessions"])["offline_protection"] is True
    monkeypatch.setattr(Reliability, "stop", _boom)
    with caplog.at_level(logging.WARNING, logger="k3code.gateway"):
        stopped = await server.sweep_idle_reliability(now=live.idle_since + server.IDLE_RELIABILITY_S + 1)
    assert stopped == 0
    assert any("stopping its idle NetWatch failed" in r.getMessage() and r.exc_info for r in caplog.records)


async def test_a_failed_reliability_stop_on_close_is_logged(tmp_path, monkeypatch, caplog):
    server = make(tmp_path, monkeypatch, REPLY, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "hi")
    monkeypatch.setattr(Reliability, "stop", _boom)
    with caplog.at_level(logging.WARNING, logger="k3code.gateway"):
        await server.close()
    assert any("stopping its reliability bundle failed" in r.getMessage() for r in caplog.records)


async def test_a_failed_empty_session_sweep_is_logged(caplog):
    async def sweep() -> int:
        raise RuntimeError("disk I/O error")

    server = SimpleNamespace(
        stopping=False,
        sweep_empty_sessions_async=sweep,
        prune_retention=lambda: {"usage_rows": 0, "journal_files": 0, "decisions": 0},
    )
    with caplog.at_level(logging.ERROR, logger="k3code.daemon"):
        await daemon._sweep_empty_sessions(server)
    assert any("empty-session sweep failed" in r.getMessage() and r.exc_info for r in caplog.records)
