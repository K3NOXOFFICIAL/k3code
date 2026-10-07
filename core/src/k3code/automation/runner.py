"""The seam between automation and the gateway: everything that needs a model, a session or the network."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

LOOP_COMPLETE = "LOOP_COMPLETE"
PACING_MIN_S = 60.0
PACING_MAX_S = 3600.0


def clamp_pacing(seconds: float) -> float:
    return max(PACING_MIN_S, min(PACING_MAX_S, float(seconds)))


@dataclass
class TickContext:
    """Filled in by the ``schedule_next`` tool during a self-paced tick."""

    next_seconds: float | None = None
    reason: str = ""

    def schedule_next(self, seconds: float, reason: str = "") -> float:
        self.next_seconds = clamp_pacing(seconds)
        self.reason = reason
        return self.next_seconds


@dataclass
class RunResult:
    status: str  # completed | failed | needs_input | interrupted
    text: str = ""
    api_calls: int = 0
    error: str = ""
    session_id: str = ""
    failure_kind: str = ""  # "" | unreachable | quota | other
    retry_after: float | None = None  # provider-stated wait (quota)
    extra: dict[str, Any] = field(default_factory=dict)


class Runner(Protocol):
    async def run_prompt(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str = "",
        model: str = "",
        name: str = "",
        mode: str = "auto",
        kind: str = "background_turn",
        tick: TickContext | None = None,
    ) -> RunResult: ...

    async def judge(self, system: str, user: str, kind: str = "goal_judge") -> str:
        """One cheap-tier completion with no tools."""
        ...

    async def run_shell(self, command: str, cwd: str) -> tuple[int, str]: ...

    async def start_goal(self, objective: str, session_id: str | None, cwd: str) -> RunResult: ...

    def notify(self, text: str, level: str = "info", key: str = "") -> None: ...


WaitOnline = Callable[[], Awaitable[Any]]

_JUDGE_SYSTEM = (
    "You judge whether a stop condition has been met. Reply with one line: `YES: <reason>` or `NO: <reason>`."
)


async def judge_condition(runner: Runner, condition: str, text: str) -> tuple[bool, str]:
    """Cheap-tier check of a ``--until`` condition against the latest tick output."""
    reply = await runner.judge(
        _JUDGE_SYSTEM, f"Condition: {condition}\n\nLatest output:\n{text[-4000:]}", "goal_judge"
    )
    m = re.match(r"\s*(yes|no)\b[:\- ]*(.*)", reply, re.I | re.S)
    if not m:
        return False, f"unparseable judge reply: {reply[:80]!r}"
    return m.group(1).lower() == "yes", m.group(2).strip()[:200]
