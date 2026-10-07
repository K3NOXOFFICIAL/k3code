"""M2-ops step 1: reliability.* / net.state loop events → the TUI events that display them."""

from __future__ import annotations

import json

from k3code.gateway.server import LiveSession
from k3code.reliability import events as ev
from k3code.reliability.events import ReliabilityEvent
from test_gateway import frames_of, make_server


def _live(server):
    stored = server.store.create(model="default", cwd="/tmp")
    live = LiveSession(stored.session_id, stored, server)
    server.session = live
    return live


def _events(server, since=0):
    return [f["params"] for f in frames_of(server)[since:] if f.get("method") == "event"]


def test_paused_emits_status_and_notification():
    server = make_server()
    live = _live(server)
    live.streaming = True
    server._on_reliability_event(live, ReliabilityEvent(ev.PAUSED, detail="network offline"))
    out = _events(server)
    types = [e["type"] for e in out]
    assert "status.update" in types and "notification.show" in types
    status = next(e for e in out if e["type"] == "status.update")
    assert status["payload"]["text"] == "⏸ offline — will resume automatically"
    assert status["payload"]["state"] == "working"  # a paused session is still working
    assert all(e.get("importance") == "essential" for e in out)
    assert live.paused and live.state == "working"


def test_parked_shows_resume_clock_time():
    server = make_server()
    live = _live(server)
    server._on_reliability_event(live, ReliabilityEvent(ev.PARKED, detail="rate limited", data={"delay": 120.0}))
    status = next(e for e in _events(server) if e["type"] == "status.update")
    assert status["payload"]["text"].startswith("⏸ waiting for provider until ")
    hhmm = status["payload"]["text"].rsplit(" ", 1)[1]
    assert len(hhmm) == 5 and hhmm[2] == ":"


def test_resumed_clears_notification_and_records_pause():
    server = make_server()
    live = _live(server)
    server._on_reliability_event(live, ReliabilityEvent(ev.PAUSED, detail="x"))
    n = len(server._frames)
    server._on_reliability_event(live, ReliabilityEvent(ev.RESUMED, detail="online"))
    out = _events(server, n)
    clear = next(e for e in out if e["type"] == "notification.clear")
    assert clear["payload"]["key"] == server.PAUSE_KEY and clear.get("importance") == "essential"
    assert not live.paused
    assert server.usage.aggregate("session")[0]["pauses"] == 1


def test_interrupted_tool_is_warning_notification():
    server = make_server()
    live = _live(server)
    server._on_reliability_event(live, ReliabilityEvent(ev.INTERRUPTED_TOOL, detail="bash (c1)"))
    note = next(e for e in _events(server) if e["type"] == "notification.show")
    assert note["payload"]["level"] == "warning" and note.get("importance") == "essential"


def test_budget_and_loop_become_error_events_and_needs_input():
    for kind, label in ((ev.BUDGET_EXCEEDED, "Budget"), (ev.LOOP_DETECTED, "Loop")):
        server = make_server()
        live = _live(server)
        server._on_reliability_event(live, ReliabilityEvent(kind, detail="why"))
        err = next(e for e in _events(server) if e["type"] == "error")
        assert label in err["payload"]["message"] and err.get("importance") == "essential"
        assert live.state == "needs_input"


def test_raw_event_is_forwarded_and_encoded_with_importance():
    server = make_server()
    live = _live(server)
    server._on_reliability_event(
        live, ReliabilityEvent(ev.NET_STATE, detail="online -> offline", data={"new": "offline"})
    )
    raw = next(e for e in _events(server) if e["type"] == "net.state")
    assert raw["payload"]["new"] == "offline" and raw["importance"] == "essential"
    json.dumps(raw)
