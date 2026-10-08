"""P0-4: ``/daemon pause`` is a persisted global halt: goal turns and loop ticks make no provider call."""

from __future__ import annotations

import asyncio
from pathlib import Path

from k3code import halt as halt_mod
from k3code.automation import engine as engine_mod
from k3code.automation.engine import AutomationEngine
from m1cmd_helpers import cmd, git_repo, make_server, new_session


async def test_halt_blocks_goal_turn_and_loop_tick_with_zero_provider_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "HALT_POLL_S", 0.05)
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=["working on it"])
    sid = await new_session(server, repo)
    live = server.live_for(server.store.get(sid))
    server.goal_manager(live).set("ship the feature")
    server.automation = AutomationEngine(server, use_netwatch=False)
    await server.automation.start()

    await server.halt_daemon("test halt")
    assert server.halted
    goal = server.goal_manager(live).state
    assert goal is not None and goal.status == "paused" and goal.paused_reason == "halted"

    assert await server._run_turn(live, "go") == ("halted", "")  # the paused goal's turn: no provider call
    assert provider.n == 0

    row = server.automation.loops.create(session_id=sid, prompt="tick the loop")  # self-paced, ticks at once
    loop_id = row["id"]
    await asyncio.sleep(0.2)  # the loop task is up, waiting on the halt
    assert provider.n == 0
    assert server.automation.db.get("loops", loop_id)["ticks"] == 0  # a halted loop does not consume a tick

    server.resume_daemon()
    for _ in range(100):
        if provider.n >= 1:
            break
        await asyncio.sleep(0.05)
    assert provider.n >= 1, "the loop tick runs after /daemon resume"
    assert server.automation.db.get("loops", loop_id)["ticks"] >= 1
    await server.close()


async def test_halt_survives_restart_and_only_resume_clears_it(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    sid = await new_session(server, repo)
    await server.halt_daemon("restart test")
    await server.close()

    server2, provider2 = make_server(tmp_path, monkeypatch, replies=["x"])  # same K3CODE_HOME: a "restarted" daemon
    assert server2.halted and server2.halt is not None and server2.halt.reason == "restart test"
    live = server2.live_for(server2.store.get(sid))
    assert (await server2._run_turn(live, "go"))[0] == "halted"
    assert provider2.n == 0

    server2.resume_daemon()
    assert not server2.halted and not halt_mod.halt_path(Path(server2._home())).exists()
    status, _ = await server2._run_turn(live, "go")
    assert status == "done" and provider2.n == 1
    await server2.close()


async def test_daemon_pause_and_resume_commands(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    sid = await new_session(server, tmp_path)
    assert "running" in str(await cmd(server, "/daemon", sid))
    paused = str(await cmd(server, "/daemon pause", sid))
    assert "halted" in paused and server.halted
    assert "HALTED" in str(await cmd(server, "/daemon", sid))
    assert "resumed" in str(await cmd(server, "/daemon resume", sid))
    assert not server.halted
    await server.close()


async def test_prompts_queued_behind_a_turn_survive_a_halt(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, replies=["x"])
    sid = await new_session(server, tmp_path)
    live = server.live_for(server.store.get(sid))
    live.pending_prompts.extend(["first queued", "second queued"])
    server.halt = halt_mod.Halt(reason="held", since=0.0)  # halted between the turn and the queue
    assert (await server._run_turn(live, "go"))[0] == "halted"
    assert live.pending_prompts == ["first queued", "second queued"] and provider.n == 0  # kept, not dropped
    server.resume_daemon()
    await server._run_turn(live, "go")
    assert live.pending_prompts == [] and provider.n == 3  # the queue runs after /daemon resume
    await server.close()
