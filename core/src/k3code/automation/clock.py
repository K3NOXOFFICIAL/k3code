"""Injectable time source: ``SystemClock`` for production, ``FakeClock`` for tests (never sleeps for real)."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class FakeClock:
    """Deterministic clock: ``sleep`` parks until ``advance`` moves time past its deadline."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = start
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self._now + seconds, fut))
        await fut

    @staticmethod
    async def settle(rounds: int = 30) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        """Move time forward, waking sleepers in deadline order and letting their tasks run in between."""
        target = self._now + seconds
        await self.settle()
        while True:
            due = sorted(((d, i) for i, (d, f) in enumerate(self._sleepers) if d <= target and not f.done()))
            if not due:
                break
            deadline, idx = due[0]
            fut = self._sleepers[idx][1]
            self._now = max(self._now, deadline)
            self._sleepers = [(d, f) for d, f in self._sleepers if f is not fut and not f.done()]
            fut.set_result(None)
            await self.settle()
        self._now = target
        self._sleepers = [(d, f) for d, f in self._sleepers if not f.done()]
        await self.settle()
