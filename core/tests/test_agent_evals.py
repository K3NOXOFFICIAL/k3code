"""Offline regression evals: each scripted task must pass within its step budget; the scoreboard sums them up.

``scripts/eval_score.py`` runs this file and prints the per-task table (see its docstring).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evals.runner import Result, run_task
from evals.tasks import TASKS

RESULTS: dict[str, Result] = {}


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
async def test_eval_task(task, tmp_path):
    RESULTS[task.id] = result = await run_task(task, tmp_path)
    assert result.passed, result.detail
    assert result.steps <= task.max_steps


async def test_eval_scoreboard(tmp_path):
    for task in TASKS:  # run on its own, or ordered first: score the tasks not run yet
        if task.id not in RESULTS:
            RESULTS[task.id] = await run_task(task, tmp_path / task.id)
    rows = [
        {"id": t.id, "passed": r.passed, "steps": r.steps, "max_steps": t.max_steps, "detail": r.detail}
        for t in TASKS
        for r in [RESULTS[t.id]]
    ]
    passed = sum(r["passed"] for r in rows)
    mean = sum(r["steps"] for r in rows) / len(rows)
    if out := os.environ.get("K3CODE_EVAL_OUT"):
        Path(out).write_text(json.dumps({"passed": passed, "total": len(rows), "mean_steps": mean, "tasks": rows}))
    print(f"\neval pass rate: {passed}/{len(rows)}, mean steps: {mean:.2f}")
