"""The limits on the model's own ``task`` calls: children at once, nested ones, per-turn count, process-wide cap."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from k3code.autonomy.agents import DAEMON_LIVE_CEILING, agent_limits
from k3code.subagents.runner import SpawnRefused, SubagentManager
from k3code.subagents.tools import register_task_tools
from test_autonomy_gateway import make, start

NO_GATE = {"plan_first": False, "proposals": False}


class Reg:
    """Captures what ``register_task_tools`` registers."""

    def __init__(self) -> None:
        self.fns: dict[str, object] = {}
        self.specs: dict[str, object] = {}

    def register(self, spec, fn) -> None:
        self.fns[spec.name] = fn
        self.specs[spec.name] = spec


@pytest.fixture
def running(monkeypatch):
    """Children that start and then wait: their handles stay live until the test finishes them."""

    async def fake_run(self, parent, h, atype, prompt, auto_merge):
        h.status = "running"
        await asyncio.sleep(60)

    monkeypatch.setattr(SubagentManager, "_run", fake_run)


async def session(tmp_path, monkeypatch, **autonomy):
    server = make(tmp_path, monkeypatch, [], autonomy={**NO_GATE, **autonomy})
    await start(server, tmp_path)
    return server, server.session


def tools(server, sess, **kw):
    reg = Reg()
    register_task_tools(reg, server, sess, **kw)
    return reg.fns["task"]


async def spawn_calls(task, n, **args):
    return [await task({"description": f"job {i}", "prompt": f"do {i}", "background": True, **args}) for i in range(n)]


async def stop_all(server, sess):
    server.subagents.interrupt_session(sess.session_id)
    await asyncio.sleep(0)


@pytest.mark.usefixtures("running")
async def test_eight_background_calls_start_four_children_and_refuse_four(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch)
    results = await spawn_calls(tools(server, sess), 8)
    ok = [r for r in results if "content" in r]
    refused = [r for r in results if "error" in r]
    assert len(ok) == 4 and len(refused) == 4
    assert "4 of 4 children are running (autonomy.agents.max_parallel)" in refused[0]["error"]
    assert "task_result(id, wait=true)" in refused[0]["error"]
    assert len(server.subagents.for_session(sess.session_id)) == 4  # a refused call leaves no handle behind
    await stop_all(server, sess)


@pytest.mark.usefixtures("running")
async def test_a_finished_child_frees_its_place(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch, agents={"max_parallel": 2})
    task = tools(server, sess)
    first, second, refused = await spawn_calls(task, 3)
    assert "content" in first and "content" in second and "error" in refused
    done = server.subagents.for_session(sess.session_id)[0]
    done.status = "completed"
    assert "content" in (await spawn_calls(task, 1))[0]
    await stop_all(server, sess)


@pytest.mark.usefixtures("running")
async def test_grandchildren_are_limited_per_child_and_can_be_switched_off(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch)
    nested = tools(server, sess, depth=2, parent_child_id="sa-parent")
    results = await spawn_calls(nested, 5)
    assert [("content" in r) for r in results] == [True, True, True, False, False]
    assert "already has 3 of 3 sub-agents running (autonomy.agents.max_nested)" in results[3]["error"]
    other = tools(server, sess, depth=2, parent_child_id="sa-other")  # another child has its own allowance
    assert "content" in (await spawn_calls(other, 1))[0]
    await stop_all(server, sess)

    (tmp_path / "off").mkdir()
    server, sess = await session(tmp_path / "off", monkeypatch, agents={"max_nested": 0})
    off = tools(server, sess, depth=2, parent_child_id="sa-parent")
    (refused,) = await spawn_calls(off, 1)
    assert "switched off (autonomy.agents.max_nested is 0)" in refused["error"]
    await stop_all(server, sess)


@pytest.mark.usefixtures("running")
async def test_the_per_turn_count_resets_with_the_next_user_turn(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch, agents={"max_spawns": 2, "max_parallel": 8})
    task = tools(server, sess)
    results = await spawn_calls(task, 3)
    assert [("content" in r) for r in results] == [True, True, False]
    assert "2 task calls already made in this turn (autonomy.agents.max_spawns)" in results[2]["error"]
    server.subagents.begin_turn(sess.session_id)
    assert "content" in (await spawn_calls(task, 1))[0]
    await stop_all(server, sess)


@pytest.mark.usefixtures("running")
async def test_zero_max_spawns_means_no_per_turn_limit(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch, agents={"max_spawns": 0, "max_parallel": 8})
    results = await spawn_calls(tools(server, sess), 8)
    assert all("content" in r for r in results)
    await stop_all(server, sess)


@pytest.mark.usefixtures("running")
async def test_the_whole_process_has_a_live_ceiling(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch)
    for i in range(DAEMON_LIVE_CEILING):  # other sessions' children
        server.subagents.handles[f"other-{i}"] = SimpleNamespace(
            parent_sid="elsewhere", parent_child_id=None, status="running", done=False
        )
    (refused,) = await spawn_calls(tools(server, sess), 1)
    assert f"{DAEMON_LIVE_CEILING} sub-agents are running across all sessions" in refused["error"]
    server.subagents.handles.clear()
    await stop_all(server, sess)


@pytest.mark.usefixtures("running")
async def test_orchestrated_children_are_never_refused_but_they_count(tmp_path, monkeypatch):
    """Fan-out and /ultracode start children directly and size themselves; the model's own calls then wait."""
    server, sess = await session(tmp_path, monkeypatch, agents={"max_parallel": 2})
    for i in range(3):
        server.subagents.spawn(sess, description=f"fan {i}", prompt="x")  # three, over the limit of two
    (refused,) = await spawn_calls(tools(server, sess), 1)
    assert "3 of 2 children are running" in refused["error"]
    await stop_all(server, sess)


@pytest.mark.usefixtures("running")
async def test_the_limits_are_read_on_every_call_so_a_session_override_applies_at_once(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch)
    task = tools(server, sess)
    assert [("content" in r) for r in await spawn_calls(task, 5)] == [True] * 4 + [False]
    sess.stored.meta["agent_limits"] = {"max_parallel": 6}  # what /maxagents parallel 6 stores
    assert all("content" in r for r in await spawn_calls(task, 2))
    await stop_all(server, sess)


async def test_admit_is_a_noop_inside_the_limits_and_raises_a_typed_error(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch)
    limits = agent_limits(server.config)
    server.subagents.admit(sess, None, limits)  # nothing running
    server.subagents.handles["x"] = SimpleNamespace(
        parent_sid=sess.session_id, parent_child_id=None, status="queued", done=False
    )
    assert server.subagents.live(sess.session_id) == 1  # queued counts as live
    with pytest.raises(SpawnRefused):
        server.subagents.admit(sess, None, agent_limits(server.config, {"max_parallel": 1}))
    server.subagents.handles.clear()


async def test_the_task_tool_spec_is_unchanged_so_the_prompt_cache_prefix_holds(tmp_path, monkeypatch):
    """The limits live in the tool's error text only: the spec the provider sees must not depend on them."""
    server, sess = await session(tmp_path, monkeypatch)
    a, b = Reg(), Reg()
    register_task_tools(a, server, sess)
    sess.stored.meta["agent_limits"] = {"max_parallel": 2, "max_agents": 5}
    register_task_tools(b, server, sess)
    assert a.specs["task"] == b.specs["task"] and a.specs["task_result"] == b.specs["task_result"]


async def test_the_task_tool_runs_in_parallel_only_for_read_only_agent_types(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch)
    when = tools(server, sess).parallel_when
    assert when({"agent_type": "explorer"}) and when({"agent_type": "planner"})
    assert not when({"agent_type": "explorer", "isolation": "worktree"})
    assert not when({"agent_type": "worker"}) and not when({})  # the default type writes
    assert not when({"agent_type": "reviewer"})  # it has bash
    assert not when({"agent_type": "no-such-type"})


@pytest.mark.parametrize("bad", [["explorer"], {"a": 1}, 3, ""])
async def test_odd_agent_types_make_a_call_serial_instead_of_raising(tmp_path, monkeypatch, bad):
    server, sess = await session(tmp_path, monkeypatch)
    when = tools(server, sess).parallel_when
    assert when({"agent_type": bad}) is False
    assert when("not a dict") is False and when(None) is False


async def test_the_batch_size_is_what_the_session_still_allows(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch, agents={"max_parallel": 4})
    limit = tools(server, sess).parallel_limit
    assert limit() == 4
    server.subagents.handles["a"] = SimpleNamespace(
        parent_sid=sess.session_id, parent_child_id=None, status="running", done=False
    )
    assert limit() == 3
    for i in range(6):
        server.subagents.handles[f"b{i}"] = SimpleNamespace(
            parent_sid=sess.session_id, parent_child_id=None, status="running", done=False
        )
    assert limit() == 1  # never below one: the admission check then answers the call
    server.subagents.handles.clear()


async def test_two_children_never_have_two_approval_questions_open(tmp_path, monkeypatch):
    server, sess = await session(tmp_path, monkeypatch)
    approve = await server._approval_callback_for(sess)
    open_now = peak = 0

    async def ask(kind, payload, sid):
        nonlocal open_now, peak
        open_now += 1
        peak = max(peak, open_now)
        await asyncio.sleep(0.05)
        open_now -= 1
        return {"choice": "once"}

    monkeypatch.setattr(server, "_ask_client", ask)
    results = await asyncio.gather(*(approve("bash", {"command": f"echo {i}"}) for i in range(4)))
    assert [r.choice for r in results] == ["once"] * 4
    assert peak == 1
