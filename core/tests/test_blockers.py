"""P1-7: blockers are durable until a client takes them; nothing waits on a person without a timeout."""

from __future__ import annotations

import asyncio

from k3code import daemon
from k3code.blockers import BlockerStore
from m1cmd_helpers import make_server
from test_daemon import Peer, _write_fake_config


async def test_blocker_with_no_client_attached_is_still_listed_after_restart(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    stored = server.store.create(cwd=str(tmp_path))  # a session no client is attached to
    server.notify_blocker(stored.session_id, "approve the deploy", kind="approval")
    assert [b["text"] for b in server.blockers.pending()] == ["approve the deploy"]
    await server.close()
    restarted = BlockerStore(tmp_path / "home" / "blockers.db")  # what the next boot sees
    assert [b["text"] for b in restarted.pending()] == ["approve the deploy"]
    restarted.close()


async def test_pending_blocker_is_handed_to_the_next_client_and_then_taken(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_fake_config(home, [{"type": "text", "text": "x"}])
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(home / "fake.json"))
    monkeypatch.setenv("FAKE_KEY", "x")
    seeded = BlockerStore(home / "blockers.db")
    seeded.add(session_id="s1", kind="approval", text="approve the deploy")
    seeded.close()
    ready, holder = asyncio.Event(), []
    task = asyncio.create_task(
        daemon.run_daemon(home=home, install_signals=False, ready_event=ready, server_out=holder, watchdog_interval=5)
    )
    await asyncio.wait_for(ready.wait(), 10)
    peer = await Peer.connect(daemon.socket_path(home))
    note = await peer.read_until("notification.show")
    assert note["payload"]["text"] == "approve the deploy"
    check = BlockerStore(home / "blockers.db")
    assert check.pending() == []  # taken by a client: no longer pending
    check.close()
    peer.close()
    holder[0].request_stop()
    await asyncio.wait_for(task, 10)


async def test_unanswered_approval_times_out_and_pauses_the_goal(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    stored = server.store.create(cwd=str(tmp_path))
    live = server.live_for(stored)
    server.goal_manager(live).set("needs an approval")
    server.approval_timeout_s = 0.05
    try:
        request = {"command": "rm -rf build", "choices": ["once", "deny"]}
        await server._ask_client("approval", request, stored.session_id)
    except RuntimeError as exc:
        assert "no answer to approval" in str(exc)
    else:
        raise AssertionError("an unanswered approval must time out")
    state = server.goal_manager(live).state
    assert state.status == "paused" and state.paused_reason == "approval timeout"
    assert any(b["kind"] == "approval" and "No answer" in b["text"] for b in server.blockers.pending())
    await server.close()
