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
    '0 candidate finding(s)'. Now the fan-out packs to two workers and the review tail (both lenses asked twice, and
    the fixer) stays paid for."""
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
    assert "pass (true)" in report and "Budget used: 10/12 agents" in report
    types = sorted(h.agent_type for h in server.subagents.handles.values())
    assert types.count("planner") == 3 and types.count("worker") == 3 and types.count("reviewer") == 4
    plan = events(server, "fanout.plan")[0]
    assert [s["title"] for s in plan["subtasks"]] == [
        "STEP-ONE add f1; then STEP-TWO add f2 (in this order)",
        "STEP-THREE add f3; then STEP-FOUR add f4 (in this order)",
    ]
    details = [e["detail"] for e in events(server, "ultra.progress") if e["phase"] == "implementing"]
    assert any("4 step(s) -> 4 unit(s) -> 2 worker(s), width 2" in d and "5 held for the review" in d for d in details)


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
    from types import SimpleNamespace

    rated = SimpleNamespace(scope="small", risk="low", needs_plan=False, source="classifier")
    report = await server.ultra.ultracode(server.session, "add one file", verdict=rated)
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
    # 3 planners + 5 held for the review + (a worker and a rework, and the reviews of both, each asked twice) for each
    # of the 2 steps = 20 of 24
    assert "/20 agents" in report and "/24 agents" not in report


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


@pytest.mark.usefixtures("sandbox_optional")
async def test_the_final_tests_are_not_repeated_on_the_tree_the_last_merge_already_tested(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    no_agreement = [
        {"type": "text", "match": "VOTE LENS: correctness", "text": json.dumps({"votes": {"f1": True, "f2": False}})},
        {
            "type": "text",
            "match": "VOTE LENS: security/robustness",
            "text": json.dumps({"votes": {"f1": False, "f2": True}}),
        },
    ]
    server = make(
        tmp_path,
        monkeypatch,
        [
            *PLANNERS,
            JUDGE,
            *worker(1, "f1.txt"),
            *worker(2, "f2.txt"),
            *worker(3, "f3.txt"),
            reviewer("STEP-ONE"),
            reviewer("STEP-TWO"),
            reviewer("STEP-THREE"),
            LENS_C,
            LENS_S,
            *no_agreement,
        ],
        mode="auto",
        autonomy=cfg(),
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "pass (true): the last merge ran it on this same tree, not repeated" in report
    assert "final tests" not in [e["phase"] for e in events(server, "ultra.progress")]


@pytest.mark.usefixtures("sandbox_optional")
async def test_the_final_tests_run_after_a_fix_changed_the_tree(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        [
            *PLANNERS,
            JUDGE,
            *worker(1, "f1.txt"),
            *worker(2, "f2.txt"),
            *worker(3, "f3.txt"),
            reviewer("STEP-ONE"),
            reviewer("STEP-TWO"),
            reviewer("STEP-THREE"),
            LENS_C,
            LENS_S,
            VOTE_C,
            VOTE_S,
            *FIXER,
        ],
        mode="auto",
        autonomy=cfg(),
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "pass (true)" in report and "not repeated" not in report
    assert "final tests" in [e["phase"] for e in events(server, "ultra.progress")]


REWORK_ONE = [
    {
        "type": "tool_call",
        "id": "w1r",
        "match": "REWORK",
        "when": "first",
        "name": "write",
        "arguments": {"path": "f1.txt", "content": "better\n"},
    },
    {"type": "text", "match": "REWORK", "when": "after_tool", "text": "worker 1 fixed"},
]


def first_review_fails_script(*extra):
    return [
        *PLANNERS,
        JUDGE,
        *worker(1, "f1.txt"),
        *worker(2, "f2.txt"),
        *worker(3, "f3.txt"),
        *REWORK_ONE,
        {"type": "text", "match": "Subtask: STEP-ONE", "text": "no test. VERDICT: fail"},
        reviewer("STEP-TWO"),
        reviewer("STEP-THREE"),
        {"type": "text", "match": "worker 1 fixed", "text": "fine now. VERDICT: pass"},
        *extra,
    ]


@pytest.mark.usefixtures("sandbox_optional")
async def test_one_failed_review_at_a_tight_budget_does_not_starve_the_other_subtasks(tmp_path, monkeypatch):
    """The rework and re-review of one subtask took the agents the others' reviewers needed: nothing merged, the panel
    never ran, and the report said the final tests passed on an unchanged tree. 14 agents is the smallest budget that
    still fans three steps into three workers (3 planners, 3 workers, 3 reviewers and the 5 held for the review)."""
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        first_review_fails_script(LENS_C, LENS_S, VOTE_C, VOTE_S, *FIXER),
        mode="auto",
        autonomy=cfg(),
        ultracode={"max_agents": 14},
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    done = events(server, "fanout.done")[0]
    states = {p["subtask_id"]: p["state"] for p in events(server, "fanout.progress")}
    assert states["t2"] == "merged" and states["t3"] == "merged", done["summary"]
    assert (repo / "f2.txt").exists() and (repo / "f3.txt").exists()
    assert "## Review panel\nnot run" not in report and "candidate finding(s)" in report
    assert states["t1"] in ("escalated", "merged")  # no agents left for its retry: it is reported, with no spend
    assert len(server.subagents.handles) <= 14
    if states["t1"] == "escalated":
        assert "## PARTIAL" in report and "[escalated] STEP-ONE add f1" in report
        assert "on 2/3 merged subtasks" in report


@pytest.mark.usefixtures("sandbox_optional")
async def test_when_nothing_merged_the_report_does_not_claim_the_tests_passed(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    idle = [{"type": "text", "match": f"YOUR subtask (t{i})", "text": "nothing to change"} for i in (1, 2, 3)]
    server = make(tmp_path, monkeypatch, [*PLANNERS, JUDGE, *idle], mode="auto", autonomy=cfg())
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "not run: nothing was merged" in report and "pass (true)" not in report
    assert "## PARTIAL" in report and "the worker made no changes" in report


@pytest.mark.usefixtures("sandbox_optional")
@pytest.mark.parametrize("max_agents", [8, 9, 10, 11, 12])
async def test_a_failed_review_never_leaves_more_than_its_own_subtask_unresolved(tmp_path, monkeypatch, max_agents):
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        first_review_fails_script(LENS_C, LENS_S, VOTE_C, VOTE_S, *FIXER),
        mode="auto",
        autonomy=cfg(),
        ultracode={"max_agents": max_agents},
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    done = events(server, "fanout.done")[0]
    unresolved = done["escalated"]
    assert done["merged"] + unresolved == done["total"]
    if done["total"] >= 3:  # a small budget packs everything into one worker, which has nothing to starve
        assert done["merged"] >= 2, done["summary"]


@pytest.mark.usefixtures("sandbox_optional")
async def test_progress_carries_elapsed_seconds_and_the_report_a_timing_line(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        [
            *PLANNERS,
            JUDGE,
            *worker(1, "f1.txt"),
            *worker(2, "f2.txt"),
            *worker(3, "f3.txt"),
            reviewer("STEP-ONE"),
            reviewer("STEP-TWO"),
            reviewer("STEP-THREE"),
            LENS_C,
            LENS_S,
            VOTE_C,
            VOTE_S,
            *FIXER,
        ],
        mode="auto",
        autonomy=cfg(),
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    elapsed = [e["elapsed_s"] for e in events(server, "ultra.progress") if e["command"] == "ultracode"]
    assert elapsed and elapsed == sorted(elapsed) and all(isinstance(x, float) for x in elapsed)
    report = server.session.stored.messages[-1]["content"]
    line = next(ln for ln in report.splitlines() if ln.startswith("Timing: "))
    for label in ("planning", "implementing", "review", "tests"):
        assert f"{label} " in line, line
    assert server.ultra._clocks == {}  # nothing left behind


def test_the_timing_line_adds_up_the_phases_that_count_towards_each_label():
    from k3code.autonomy.ultra import timing_line

    marks = [
        ("start", 0.0),
        ("planning", 0.0),
        ("judging", 10.0),
        ("implementing", 25.0),
        ("adversarial review", 100.0),
        ("cross-checking findings", 110.0),
        ("final tests", 130.0),
    ]
    assert timing_line(marks, 190.0) == "planning 25s, implementing 75s, review 30s, tests 60s"
    assert timing_line([("start", 0.0), ("planning", 1.0), ("stopped", 5.0)], 9.0) == "planning 4s"
    assert timing_line([], 5.0) == ""


@pytest.mark.usefixtures("sandbox_optional")
async def test_the_final_tests_run_when_the_session_is_in_a_subdirectory_of_the_repo(tmp_path, monkeypatch):
    """The fan-out tests the repository root; a run started in a subdirectory tests that directory at the end."""
    repo = git_repo(tmp_path / "repo")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "keep.txt").write_text("x\n")
    from test_fanout import git

    git(repo, "add", "-A")
    git(repo, "commit", "-m", "pkg")
    server = make(
        tmp_path,
        monkeypatch,
        [
            *PLANNERS,
            JUDGE,
            *worker(1, "f1.txt"),
            *worker(2, "f2.txt"),
            *worker(3, "f3.txt"),
            reviewer("STEP-ONE"),
            reviewer("STEP-TWO"),
            reviewer("STEP-THREE"),
            LENS_C,
            LENS_S,
            {
                "type": "text",
                "match": "VOTE LENS: correctness",
                "text": json.dumps({"votes": {"f1": True, "f2": False}}),
            },
            {
                "type": "text",
                "match": "VOTE LENS: security/robustness",
                "text": json.dumps({"votes": {"f1": False, "f2": True}}),
            },
        ],
        mode="auto",
        autonomy=cfg(),
    )
    await call(server, "session.create", {"cwd": str(repo / "pkg")})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "not repeated" not in report
    assert "final tests" in [e["phase"] for e in events(server, "ultra.progress")]


async def test_when_the_only_planner_fails_the_next_angle_is_tried_and_the_run_goes_on(tmp_path, monkeypatch):
    from types import SimpleNamespace

    repo = git_repo(tmp_path / "repo")
    rated = SimpleNamespace(scope="small", risk="low", needs_plan=False, source="classifier")
    script = [
        {"type": "text", "match": "ANGLE: mvp-first", "text": ""},  # the one planner says nothing
        {"type": "text", "match": "ANGLE: risk-first", "text": "RISK-PLAN: guard f1"},
        JUDGE,
        *worker(1, "f1.txt"),
        reviewer("add one file"),
        {"type": "text", "match": "LENS:", "text": "[]"},
    ]
    server = make(tmp_path, monkeypatch, script, mode="auto", autonomy=cfg())
    await call(server, "session.create", {"cwd": str(repo)})
    report = await server.ultra.ultracode(server.session, "add one file", verdict=rated)
    planners = [h.description for h in server.subagents.handles.values() if h.agent_type == "planner"]
    assert planners == ["plan: mvp-first", "plan: risk-first"]
    assert not any("plan judge" in c["text"] for p in server.providers for c in p.log)  # one plan: nothing to judge
    assert (repo / "f1.txt").exists() and "Review panel" in report


async def test_a_task_the_safety_floor_touched_keeps_all_three_planners(tmp_path, monkeypatch):
    from types import SimpleNamespace

    repo = git_repo(tmp_path / "repo")
    risky = SimpleNamespace(scope="small", risk="high", needs_plan=True, source="classifier")
    server = make(tmp_path, monkeypatch, [*PLANNERS, JUDGE], mode="auto", autonomy=cfg(), ultracode={"max_agents": 3})
    await call(server, "session.create", {"cwd": str(repo)})
    await server.ultra.ultracode(server.session, "rotate the production credentials", verdict=risky)
    assert len([h for h in server.subagents.handles.values() if h.agent_type == "planner"]) == 3


# ── a rework needs two agents: itself and its re-review ──

ONE_STEP = (
    "## Goal\nAdd a file\n## Steps\n1. STEP-ONE add f1\n## Files\nnone\n## Risks\nnone\n"
    "## Verification\nfile exists\n## Estimate\nsmall"
)


def rework_of(i: int, name: str) -> list[dict]:
    """What worker ``i`` does when the reviewer's rejection ("t<i> needs a test") is sent back to it."""
    key = f"t{i} needs a test"
    return [
        {
            "type": "tool_call",
            "id": f"r{i}",
            "match": key,
            "when": "first",
            "name": "write",
            "arguments": {"path": name, "content": "better\n"},
        },
        {"type": "text", "match": key, "when": "after_tool", "text": f"reworked {i}"},
    ]


def two_step_rework_script(*, both_rejected: bool, second_takes: float = 0.0):
    """Two steps; STEP-ONE's first review fails (STEP-TWO's too when ``both_rejected``), each rework then passes.
    ``second_takes``: seconds STEP-TWO's worker works, so that STEP-ONE's review comes back first."""
    two_judge = {**JUDGE, "text": 'SCORES: {"mvp-first": 6}\n' + TWO_STEPS}
    fails = [{"type": "text", "match": "Subtask: STEP-ONE", "text": "t1 needs a test. VERDICT: fail"}]
    passes = [{"type": "text", "match": "reworked 1", "text": "fine now. VERDICT: pass"}]
    reworks = rework_of(1, "f1.txt")
    if both_rejected:
        fails.append({"type": "text", "match": "Subtask: STEP-TWO", "text": "t2 needs a test. VERDICT: fail"})
        passes.append({"type": "text", "match": "reworked 2", "text": "fine now. VERDICT: pass"})
        reworks += rework_of(2, "f2.txt")
    else:
        fails.append(reviewer("STEP-TWO"))
    return [
        *PLANNERS,
        two_judge,
        *worker(1, "f1.txt"),
        *worker(2, "f2.txt", sleep=second_takes),
        *reworks,
        *fails,
        *passes,
        LENS_C,
        LENS_S,
        VOTE_C,
        VOTE_S,
        *FIXER,
    ]


@pytest.mark.usefixtures("sandbox_optional")
@pytest.mark.parametrize("both_rejected", [False, True], ids=["one-rejected", "both-rejected"])
async def test_max_agents_auto_pays_for_a_rework_and_its_rereview(tmp_path, monkeypatch, both_rejected):
    """`auto` trimmed the budget to three agents per worker, so a rejected subtask's rework ran, its re-review was
    refused and nothing merged. Both rejected: one of the two used to be left unmerged."""
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        two_step_rework_script(both_rejected=both_rejected),
        mode="auto",
        autonomy=cfg(),
        ultracode={"max_agents": "auto"},
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add two files")
    report = server.session.stored.messages[-1]["content"]
    states = {p["subtask_id"]: (p["state"], p["detail"]) for p in events(server, "fanout.progress")}
    assert [state for state, _ in states.values()] == ["merged", "merged"], states
    assert "agent budget exhausted" not in report and "STOPPED" not in report
    reworks = [h.description for h in server.subagents.handles.values() if h.description.startswith("fix: STEP")]
    assert len(reworks) == (2 if both_rejected else 1)


@pytest.mark.usefixtures("sandbox_optional")
async def test_max_agents_auto_pays_for_the_rework_after_a_failed_merge_and_its_rereview(tmp_path, monkeypatch):
    """The merge-rework path: the first merge's tests fail, the worker is reworked, and the rework's re-review used
    to be refused (6 of 9 agents used, 3 held back), so the rework was lost."""
    repo = git_repo(tmp_path / "repo")
    one = {**JUDGE, "text": 'SCORES: {"mvp-first": 6}\n' + ONE_STEP}
    flaky = "mkdir -p .k3code && (test -f .k3code/ran || (touch .k3code/ran; exit 1))"  # fails on the first merge only
    script = [
        *PLANNERS,
        one,
        *worker(1, "f1.txt"),
        *REWORK_ONE,
        reviewer("add a file"),
        LENS_C,
        LENS_S,
        VOTE_C,
        VOTE_S,
        *FIXER,
    ]
    fan = {"enabled": True, "max_parallel": 3, "require_tests": True, "test_command": flaky}
    server = make(
        tmp_path, monkeypatch, script, mode="auto", autonomy=cfg(fanout=fan), ultracode={"max_agents": "auto"}
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add a file")
    report = server.session.stored.messages[-1]["content"]
    states = [(p["state"], p["detail"][:60]) for p in events(server, "fanout.progress")]
    assert [state for state, _ in states][-1] == "merged" and "retrying" in [state for state, _ in states], states
    assert "1/1 subtasks merged" in report and "agent budget exhausted" not in report


@pytest.mark.usefixtures("sandbox_optional")
async def test_a_rework_whose_rereview_the_budget_cannot_pay_is_not_started(tmp_path, monkeypatch):
    """Both subtasks are rejected; 15 agents leave three free ones after the planners, the workers and their first
    reviewers (the tail is held). The first rework takes two (itself and its re-review). The second saw one left, ran
    its rework anyway and was then refused its re-review: an agent spent for a result that could not merge."""
    repo = git_repo(tmp_path / "repo")
    script = two_step_rework_script(both_rejected=True, second_takes=0.4)
    server = make(tmp_path, monkeypatch, script, mode="auto", autonomy=cfg(), ultracode={"max_agents": 15})
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add two files")
    report = server.session.stored.messages[-1]["content"]
    states = {p["subtask_id"]: (p["state"], p["detail"]) for p in events(server, "fanout.progress")}
    assert states["t1"][0] == "merged", states
    assert states["t2"][0] == "escalated" and "a rework and its re-review need 2 more" in states["t2"][1], states
    assert "t2 needs a test" in states["t2"][1]  # the rejection is kept for the parent
    handles = [h.description for h in server.subagents.handles.values()]
    assert "fix: STEP-ONE add f1" in handles and "fix: STEP-TWO add f2" not in handles
    assert "Budget used: 12/15 agents" in report and "[escalated] STEP-TWO add f2" in report
