"""Shared fakes for the automation tests."""

from __future__ import annotations

from typing import Any

from k3code.automation.clock import FakeClock
from k3code.automation.runner import RunResult, TickContext
from k3code.automation.store import AutomationDB


class FakeRunner:
    """Scripted runner: ``results`` is consumed one per run_prompt; the last one repeats."""

    def __init__(self, results: list[RunResult] | None = None, judge_replies: list[str] | None = None) -> None:
        self.results = results or [RunResult(status="completed", text="ok", api_calls=1)]
        self.judge_replies = judge_replies or ["NO: not yet"]
        self.judge_kinds: list[str] = []
        self.prompts: list[dict[str, Any]] = []
        self.notes: list[tuple[str, str]] = []
        self.shells: list[tuple[str, str]] = []
        self.goals: list[str] = []
        self.pace: list[float | None] = []  # per-run: seconds the "model" asks for (self-paced)

    async def run_prompt(
        self,
        prompt,
        *,
        session_id=None,
        cwd="",
        model="",
        name="",
        mode="auto",
        tick: TickContext | None = None,
        kind="background_turn",
    ):
        i = len(self.prompts)
        self.prompts.append(
            {
                "prompt": prompt,
                "session_id": session_id,
                "cwd": cwd,
                "model": model,
                "name": name,
                "mode": mode,
                "kind": kind,
            }
        )
        if tick is not None and i < len(self.pace) and self.pace[i] is not None:
            tick.schedule_next(self.pace[i], "test")
        return self.results[min(i, len(self.results) - 1)]

    async def judge(self, system: str, user: str, kind: str = "goal_judge") -> str:
        self.judge_kinds.append(kind)
        i = min(len(self.judge_replies) - 1, getattr(self, "_j", 0))
        self._j = getattr(self, "_j", 0) + 1
        return self.judge_replies[i]

    async def run_shell(self, command: str, cwd: str):
        self.shells.append((command, cwd))
        return 0, "ok"

    async def start_goal(self, objective: str, session_id, cwd: str):
        self.goals.append(objective)
        return RunResult(status="completed", text="goal started")

    def notify(self, text: str, level: str = "info", key: str = "") -> None:
        self.notes.append((text, level))


def make_db(tmp_path) -> AutomationDB:
    return AutomationDB(tmp_path / "automation.db")


def clock() -> FakeClock:
    return FakeClock(1_700_000_000.0)
