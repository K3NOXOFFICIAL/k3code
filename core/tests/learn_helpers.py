"""Shared fakes for the M5 learning tests: fake clock, fake model caller, decision seeding."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from k3code.learning.decisions import DecisionLog


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


class FakeCaller:
    """Returns scripted texts (last one repeats); records the prompts it saw."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts) or [""]
        self.calls: list[list[Any]] = []

    async def complete(self, kind: Any, messages: list[Any], **kw: Any) -> Any:
        self.calls.append(messages)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return SimpleNamespace(text=text)


def approve(log: DecisionLog, pattern: str, choice: str = "once", cwd: str = "/p/one", tool: str = "bash",
            n: int = 1, session: str = "s") -> None:
    for i in range(n):
        log.record("approval", session=f"{session}{i}", cwd=cwd, subject=pattern, choice=choice,
                   detail={"tool": tool}, project=f"path:{cwd}")
