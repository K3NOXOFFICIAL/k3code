"""PlanFirst.drain must return when every tracked task is done, even before their done-callbacks ran."""

from __future__ import annotations

import asyncio

from k3code.autonomy.plan_first import PlanFirst


async def test_drain_returns_when_tracked_tasks_finished_but_callbacks_have_not_run() -> None:
    pf = PlanFirst.__new__(PlanFirst)
    pf._tasks = set()

    async def noop() -> None:
        return None

    done = asyncio.get_running_loop().create_task(noop())
    await asyncio.wait({done})
    pf._tasks.add(done)  # finished, but nothing will ever call discard for it (callback already gone)
    await asyncio.wait_for(pf.drain(), timeout=2)
    assert not pf._tasks


async def test_drain_waits_for_running_tasks_and_ones_they_spawn() -> None:
    pf = PlanFirst.__new__(PlanFirst)
    pf._tasks = set()
    order: list[str] = []

    async def second() -> None:
        await asyncio.sleep(0.01)
        order.append("second")

    async def first() -> None:
        await asyncio.sleep(0.01)
        order.append("first")
        pf.spawn(second())

    pf.spawn(first())
    await asyncio.wait_for(pf.drain(), timeout=2)
    assert order == ["first", "second"]
    assert not pf._tasks
