"""A server that is down or hangs must not hold every turn; start-up overlaps the hooks instead of following them."""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

from k3code import mcpclient
from k3code.config import McpServerConfig
from k3code.mcpclient import McpManager
from m1cmd_helpers import make_server

HANG = McpServerConfig(command=sys.executable, args=["-c", "import time; time.sleep(60)"])
MISSING = McpServerConfig(command="/nonexistent/k3code-test-server")


async def test_a_server_that_cannot_start_fails_at_once(monkeypatch):
    manager = McpManager({"gone": MISSING})
    began = time.monotonic()
    try:
        await manager.ensure_started()
        (state,) = manager.states()
        assert state.status == "failed" and state.error
        assert time.monotonic() - began < 5  # not the 35 s wait for a connect that was never going to happen
    finally:
        await manager.close()


async def test_a_hanging_server_is_not_waited_for_again_on_every_turn(monkeypatch):
    monkeypatch.setattr(mcpclient, "CONNECT_TIMEOUT", 0.6)
    monkeypatch.setattr(mcpclient, "REVIVE_WAIT_S", 0.3)
    manager = McpManager({"hang": HANG})
    try:
        began = time.monotonic()
        await manager.ensure_started()
        first = time.monotonic() - began
        (state,) = manager.states()
        assert state.status == "failed" and first < 3  # released at the failure, not after the teardown
        # the next turns: the throttle counts from the failure, so nothing is relaunched and nothing is waited for
        for _ in range(3):
            began = time.monotonic()
            await manager.ensure_started()
            assert time.monotonic() - began < 0.2
        assert manager.states()[0].status == "failed"
        # once the pause is over a turn relaunches it but waits only the short revive time for it
        monkeypatch.setattr(mcpclient, "RESTART_BACKOFF_S", 0.05)
        await asyncio.sleep(0.1)
        began = time.monotonic()
        await manager.ensure_started()
        waited = time.monotonic() - began
        assert 0.2 < waited < 0.55, waited  # REVIVE_WAIT_S (0.3), not the 0.6 s connect timeout
        assert manager.states()[0].status == "pending"  # still starting: it connects (or fails) in the background
    finally:
        await manager.close()


async def test_a_server_that_died_after_running_a_while_is_relaunched_at_once(monkeypatch):
    manager = McpManager({"s": MISSING})
    try:
        await manager.ensure_started()
        runner = manager._runners["s"]
        runner.connected_once = True  # it had been up
        runner.started_at = time.monotonic() - 3600  # ... for an hour
        old = runner
        await manager.ensure_started()
        assert manager._runners["s"] is not old  # relaunched without the 30 s pause a never-connected one gets
    finally:
        await manager.close()


async def test_cold_says_when_the_servers_still_need_starting():
    assert McpManager({}).cold is False
    assert McpManager({"s": McpServerConfig(command="x", enabled=False)}).cold is False
    manager = McpManager({"s": MISSING})
    assert manager.cold is True
    try:
        await manager.ensure_started()
        assert manager.cold is False
        manager.configure({"s": McpServerConfig(command="/nonexistent/other")})
        assert manager.cold is True  # a changed definition restarts them
    finally:
        await manager.close()


async def test_an_early_start_and_the_turns_own_call_start_the_servers_once(monkeypatch):
    launches: list[str] = []
    manager = McpManager({"s": MISSING})
    real = manager._launch

    def counting(name, cfg):
        launches.append(name)
        return real(name, cfg)

    monkeypatch.setattr(manager, "_launch", counting)
    try:
        await asyncio.gather(manager.start_early(), manager.ensure_started(), manager.ensure_started())
        assert launches == ["s"]
    finally:
        await manager.close()


async def test_a_turn_kicks_off_the_servers_before_its_hooks_and_only_when_they_are_cold(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["unused"])
    started: list[str] = []

    async def start_early():
        started.append("early")

    async def close():
        pass

    server.mcp = SimpleNamespace(cold=True, start_early=start_early, close=close)
    server._kick_mcp()
    await asyncio.sleep(0)
    assert started == ["early"]
    server.mcp = SimpleNamespace(cold=False, start_early=start_early, close=close)
    server._kick_mcp()
    server.halt = object()  # /daemon pause
    server.mcp = SimpleNamespace(cold=True, start_early=start_early, close=close)
    server._kick_mcp()  # /daemon pause: nothing starts
    await asyncio.sleep(0)
    assert started == ["early"]
    await server.close()


async def test_a_failing_early_start_does_not_leave_an_unretrieved_error(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["unused"])
    seen: list[dict] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _l, ctx: seen.append(ctx))

    async def boom():
        raise RuntimeError("no mcp")

    async def close():
        pass

    server.mcp = SimpleNamespace(cold=True, start_early=boom, close=close)
    server._kick_mcp()
    await asyncio.sleep(0.05)
    import gc

    gc.collect()
    assert seen == []  # the turn's own ensure_started reports a failure; the early one stays quiet
    await server.close()
