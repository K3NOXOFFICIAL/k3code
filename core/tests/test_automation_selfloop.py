"""Issue #159: a file_change automation must not re-trigger on its own edits, and event-driven model runs get a
default rate limit."""

from __future__ import annotations

import asyncio

from auto_helpers import FakeRunner, clock, make_db
from k3code.automation.automations import DEFAULT_MAX_PER_HOUR, AutomationManager
from k3code.automation.triggers import FileChangeTrigger


async def test_file_change_does_not_refire_on_the_runs_own_edits(tmp_path):
    events: list[dict] = []

    async def fire(info):
        events.append(info)
        if len(events) == 1:
            (tmp_path / "generated.py").write_text("x = 1")  # what the action itself writes
            await asyncio.sleep(0.2)

    t = FileChangeTrigger({"type": "file_change", "glob": "*.py", "debounce": 0.05}, fire, clock(), cwd=str(tmp_path))
    t.start()
    try:
        await asyncio.wait_for(t.ready.wait(), 5)
        await asyncio.sleep(0.3)  # let inotify arm
        (tmp_path / "a.py").write_text("y = 2")
        for _ in range(200):
            if events:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(1.5)  # the run's own edit is delivered right after the run
        assert [e["path"] for e in events] == ["a.py"]
        (tmp_path / "later.py").write_text("z = 3")  # a later edit still fires
        for _ in range(200):
            if len(events) > 1:
                break
            await asyncio.sleep(0.02)
        assert [e["path"] for e in events] == ["a.py", "later.py"]
    finally:
        await t.stop()


async def _fires(tmp_path, trigger, action, policy=None, n=DEFAULT_MAX_PER_HOUR + 2) -> int:
    db = make_db(tmp_path)
    m = AutomationManager(db, FakeRunner(), clock())
    a = m.add(name="x", trigger=trigger, action=action, policy=policy)
    for _ in range(n):
        await m.fire(a["id"], {"event": "x"})
    await m.stop()
    return db.get("automations", a["id"])["fire_count"]


async def test_event_driven_prompt_and_goal_runs_get_a_default_rate_limit(tmp_path):
    net = {"type": "net_state"}
    assert await _fires(tmp_path / "p", net, {"type": "prompt", "prompt": "p"}) == DEFAULT_MAX_PER_HOUR
    assert await _fires(tmp_path / "g", net, {"type": "goal", "objective": "o"}) == DEFAULT_MAX_PER_HOUR


async def test_the_default_rate_limit_can_be_lifted_and_spares_cron_and_cheap_actions(tmp_path):
    n = DEFAULT_MAX_PER_HOUR + 2
    net, prompt = {"type": "net_state"}, {"type": "prompt", "prompt": "p"}
    assert await _fires(tmp_path / "zero", net, prompt, {"max_per_hour": 0}) == n
    assert await _fires(tmp_path / "set", net, prompt, {"max_per_hour": 2}) == 2
    assert await _fires(tmp_path / "cron", {"type": "cron", "schedule": "*/5 * * * *"}, prompt) == n
    assert await _fires(tmp_path / "shell", net, {"type": "shell", "command": "true"}) == n
