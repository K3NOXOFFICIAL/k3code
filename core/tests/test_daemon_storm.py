"""P1-3: restart-storm safe mode lifts itself after 30 quiet minutes, at most 3 times per 24 h, never over a halt."""

from __future__ import annotations

from k3code import daemon
from m1cmd_helpers import make_server

T0 = 1_700_000_000.0


def _storm(home, server, start: float) -> None:
    """Six starts in a row: the sixth enters safe mode, as run_daemon would."""
    count = 0
    for i in range(6):
        count = daemon.record_restart(home, now=start + i)
    server.background_paused = daemon.storm_active(count)
    assert server.background_paused


def _notices(server, key: str) -> list[dict]:
    shown = [e["payload"] for e in server.event_log if e["type"] == "notification.show"]
    return [p for p in shown if p.get("key") == key]


def test_six_starts_then_thirty_quiet_minutes_clear_it_with_one_notification(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    home = tmp_path / "home"
    _storm(home, server, T0)
    assert not daemon.maybe_auto_clear(server, home, now=T0 + 5 + 1799)  # 29 min 59 s quiet: still paused
    assert server.background_paused
    assert daemon.maybe_auto_clear(server, home, now=T0 + 5 + 1800)
    assert not server.background_paused and server.safe_mode_notice == ""
    assert len(_notices(server, "k3.safe_mode.cleared")) == 1
    assert not daemon.maybe_auto_clear(server, home, now=T0 + 5 + 3600)  # nothing left to clear: no second notice
    assert len(_notices(server, "k3.safe_mode.cleared")) == 1


async def test_a_fourth_auto_clear_within_a_day_stays_paused_until_a_human_resumes(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    home = tmp_path / "home"
    for cycle in range(3):  # three storms, an hour apart, each cleared after 30 quiet minutes
        base = T0 + cycle * 3600
        _storm(home, server, base)
        assert daemon.maybe_auto_clear(server, home, now=base + 5 + 1800)
    base = T0 + 3 * 3600
    _storm(home, server, base)
    assert not daemon.maybe_auto_clear(server, home, now=base + 5 + 1800)  # the 4th within 24 h: no
    assert server.background_paused and "clear limit" in server.safe_mode_notice
    assert daemon.maybe_auto_clear(server, home, now=base + 5 + 1800 + 86400)  # a day later the window has rolled
    _storm(home, server, base + 86400 + 3600)
    server.resume_daemon()  # the human path always works
    assert not server.background_paused
    await server.close()


async def test_a_halt_overrides_the_auto_clear(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    home = tmp_path / "home"
    _storm(home, server, T0)
    await server.halt_daemon("held")
    assert not daemon.maybe_auto_clear(server, home, now=T0 + 5 + 7200)
    assert server.background_paused
    await server.close()
