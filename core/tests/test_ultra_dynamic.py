"""/ultracode with dynamic agent sizing: the review tail is kept, small tasks use fewer planners, votes run at once."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from env_fixtures import sandbox_optional  # noqa: F401  (a fixture)
from m1cmd_helpers import git_repo
from test_autonomy_gateway import call, events, make
from test_fanout import reviewer, worker
from test_ultra import FIXER, JUDGE, LENS_C, LENS_S, PLANNERS, VOTE_C, VOTE_S, cfg, run_job

FOUR_STEPS = (
    "## Goal\nAdd four files\n## Steps\n1. STEP-ONE add f1 (parallel)\n2. STEP-TWO add f2 (parallel)\n"
    "3. STEP-THREE add f3 (parallel)\n4. STEP-FOUR add f4 (parallel)\n## Files\nnone\n## Risks\nnone\n"
    "## Verification\nfiles exist\n## Estimate\nlarge"
)
FOUR_JUDGE = {
    "type": "text",
    "match": "You are the plan judge",
    "text": 'SCORES: {"mvp-first": 6, "risk-first": 8, "architecture-first": 7}\n' + FOUR_STEPS,
}
TWO_STEPS = (
    "## Goal\nAdd two files\n## Steps\n1. STEP-ONE add f1 (parallel)\n2. STEP-TWO add f2 (parallel)\n"
    "## Files\nnone\n## Risks\nnone\n## Verification\nfiles exist\n## Estimate\nlarge"
)


def four_step_script(*extra):
    """Four steps. Packed into three workers, the first one carries STEP-ONE and STEP-TWO."""
    return [
        *PLANNERS,
        FOUR_JUDGE,
        *worker(1, "f1.txt"),
        *worker(2, "f3.txt"),
        *worker(3, "f4.txt"),
        reviewer("STEP-ONE"),
        reviewer("STEP-THREE"),
        reviewer("STEP-FOUR"),
        *extra,
    ]


@pytest.mark.usefixtures("sandbox_optional")
async def test_four_subtasks_at_the_default_budget_still_get_the_review_panel_and_the_tests(tmp_path, monkeypatch):
    """Before: 4 workers + 4 reviewers + 3 planners = 11 of 12; the panel ran out of agents and the report said
    '0 candidate finding(s)'. Now the fan-out packs to three workers and the review tail stays paid for."""
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        four_step_script(LENS_C, LENS_S, VOTE_C, VOTE_S, *FIXER),
        mode="auto",
        autonomy=cfg(),
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add four files")
    report = server.session.stored.messages[-1]["content"]
    assert "STOPPED" not in report
    assert "2 candidate finding(s); 1 confirmed by both reviewers" in report
    assert "pass (true)" in report and "Budget used: 12/12 agents" in report
    types = sorted(h.agent_type for h in server.subagents.handles.values())
    assert types.count("planner") == 3 and types.count("worker") == 4 and types.count("reviewer") == 5
    plan = events(server, "fanout.plan")[0]
    assert [s["title"] for s in plan["subtasks"]] == [
        "STEP-ONE add f1; then STEP-TWO add f2 (in this order)",
        "STEP-THREE add f3",
        "STEP-FOUR add f4",
    ]
    details = [e["detail"] for e in events(server, "ultra.progress") if e["phase"] == "implementing"]
    assert any("4 step(s) -> 4 unit(s) -> 3 worker(s), width 3" in d and "3 held for the review" in d for d in details)


async def test_with_dynamic_sizing_off_the_old_shape_runs_and_the_report_says_the_panel_did_not_run(
    tmp_path, monkeypatch
):
    repo = git_repo(tmp_path / "repo")
    script = [
        *PLANNERS,
        FOUR_JUDGE,
        *worker(1, "f1.txt"),
        *worker(2, "f2.txt"),
        *worker(3, "f3.txt"),
        *worker(4, "f4.txt"),
        reviewer("STEP-ONE"),
        reviewer("STEP-TWO"),
        reviewer("STEP-THREE"),
        reviewer("STEP-FOUR"),
        LENS_C,
        LENS_S,
        VOTE_C,
        VOTE_S,
    ]
    server = make(
        tmp_path,
        monkeypatch,
        script,
        mode="auto",
        autonomy=cfg(agents={"dynamic": False}),
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add four files")
    report = server.session.stored.messages[-1]["content"]
    assert [h.agent_type for h in server.subagents.handles.values()].count("worker") == 4  # one per step, as before
    assert "STOPPED" in report and "agent budget exhausted" in report
    assert "## Review panel\nnot run" in report and "0 candidate" not in report


async def test_a_small_scope_runs_one_planner_and_no_judge(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    script = [
        *PLANNERS,
        JUDGE,
        *worker(1, "f1.txt"),
        reviewer("add one file"),
        {"type": "text", "match": "LENS:", "text": "[]"},
    ]
    server = make(tmp_path, monkeypatch, script, mode="auto", autonomy=cfg())
    await call(server, "session.create", {"cwd": str(repo)})
    report = await server.ultra.ultracode(server.session, "add one file", scope="small")
    planners = [h for h in server.subagents.handles.values() if h.agent_type == "planner"]
    assert [h.description for h in planners] == ["plan: mvp-first"]
    calls = [c for p in server.providers for c in p.log]
    assert not any("plan judge" in c["text"] for c in calls)
    assert "Review panel" in report and (repo / "f1.txt").exists()


async def test_a_task_without_a_verdict_keeps_all_three_planners_and_the_judge(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server = make(tmp_path, monkeypatch, [*PLANNERS, JUDGE], mode="auto", autonomy=cfg(), ultracode={"max_agents": 3})
    await call(server, "session.create", {"cwd": str(repo)})
    await server.ultra.ultracode(server.session, "add three files")  # no scope: the explicit /ultracode <task> path
    assert sorted(h.description for h in server.subagents.handles.values()) == [
        "plan: architecture-first",
        "plan: mvp-first",
        "plan: risk-first",
    ]


@pytest.mark.parametrize("dynamic", [True, False])
async def test_max_agents_auto_is_trimmed_to_what_the_plan_uses(tmp_path, monkeypatch, dynamic):
    repo = git_repo(tmp_path / "repo")
    two_judge = {**JUDGE, "text": 'SCORES: {"mvp-first": 6}\n' + TWO_STEPS}
    script = [
        *PLANNERS,
        two_judge,
        *worker(1, "f1.txt"),
        *worker(2, "f2.txt"),
        reviewer("STEP-ONE"),
        reviewer("STEP-TWO"),
        LENS_C,
        LENS_S,
        VOTE_C,
        VOTE_S,
        *FIXER,
    ]
    server = make(
        tmp_path,
        monkeypatch,
        script,
        mode="auto",
        autonomy=cfg(agents={"dynamic": dynamic}),
        ultracode={"max_agents": "auto"},
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add two files")
    report = server.session.stored.messages[-1]["content"]
    assert "STOPPED" not in report
    # 3 planners + 3 held for the review + (a worker, its reviewer and one rework) for each of the 2 steps = 12 of 24
    assert "/12 agents" in report and "/24 agents" not in report


async def test_a_session_override_changes_the_budget_of_the_next_run(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server = make(tmp_path, monkeypatch, [*PLANNERS, JUDGE], mode="auto", autonomy=cfg())
    await call(server, "session.create", {"cwd": str(repo)})
    server.session.stored.meta["agent_limits"] = {"max_agents": 3}  # what /maxagents 3 stores
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "STOPPED" in report and "agent budget exhausted (3/3 agents)" in report


async def test_the_workers_of_a_dynamic_run_respect_the_sessions_parallel_limit(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    script = [
        *PLANNERS,
        JUDGE,
        *worker(1, "f1.txt", sleep=0.4),
        *worker(2, "f2.txt", sleep=0.4),
        *worker(3, "f3.txt", sleep=0.4),
        reviewer("STEP-ONE"),
        reviewer("STEP-TWO"),
        reviewer("STEP-THREE"),
    ]
    server = make(tmp_path, monkeypatch, script, mode="auto", autonomy=cfg(agents={"max_parallel": 2}))
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    assert events(server, "fanout.plan")[0]["max_parallel"] == 2


@pytest.mark.usefixtures("sandbox_optional")
async def test_the_two_cross_check_votes_run_at_the_same_time(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        [*PLANNERS, JUDGE, *worker(1, "f1.txt"), *worker(2, "f2.txt"), *worker(3, "f3.txt")]
        + [reviewer("STEP-ONE"), reviewer("STEP-TWO"), reviewer("STEP-THREE"), LENS_C, LENS_S, VOTE_C, VOTE_S, *FIXER],
        mode="auto",
        autonomy=cfg(),
    )
    await call(server, "session.create", {"cwd": str(repo)})
    caller = server.model_caller
    real = caller.complete
    live = peak = 0
    spans: list[tuple[float, float]] = []

    async def slow(kind, messages, **kw):
        nonlocal live, peak
        if "VOTE LENS" not in messages[-1].content:
            return await real(kind, messages, **kw)
        live += 1
        peak = max(peak, live)
        start = time.monotonic()
        try:
            await asyncio.sleep(0.3)
            return await real(kind, messages, **kw)
        finally:
            live -= 1
            spans.append((start, time.monotonic()))

    monkeypatch.setattr(caller, "complete", slow)
    await run_job(server, "ultracode add three files")
    assert peak == 2, "the two votes should overlap"
    assert max(e for _, e in spans) - min(s for s, _ in spans) < 0.58  # two 0.3 s calls would take 0.6 s in a row
    report = server.session.stored.messages[-1]["content"]
    assert "1 confirmed by both reviewers" in report
    json.dumps(report)  # the report stays plain text
