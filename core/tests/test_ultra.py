"""/ultraplan (3 plans + judge + file), /go with fan-out, /ultracode review panel and budget guards."""

from __future__ import annotations

import json

from k3code.autonomy.ultra import (
    dedupe_findings,
    parse_findings,
    parse_judge,
    parse_votes,
)
from m1cmd_helpers import git_repo
from test_autonomy_gateway import call, events, make
from test_fanout import git, reviewer, worker

FINAL_PLAN = (
    "## Goal\nAdd three files\n## Steps\n1. STEP-ONE add f1 (parallel)\n2. STEP-TWO add f2 (parallel)\n"
    "3. STEP-THREE add f3 (parallel)\n## Files\nf1.txt f2.txt f3.txt\n## Risks\nnone\n## Verification\nfiles exist\n"
    "## Estimate\nsmall"
)
PLANNERS = [
    {"type": "text", "match": "ANGLE: mvp-first", "text": "MVP-PLAN: just f1"},
    {"type": "text", "match": "ANGLE: risk-first", "text": "RISK-PLAN: guard f2"},
    {"type": "text", "match": "ANGLE: architecture-first", "text": "ARCH-PLAN: layout f3"},
    {"type": "usage", "match": "ANGLE:", "prompt_tokens": 100, "completion_tokens": 50},
]
JUDGE = {
    "type": "text",
    "match": "You are the plan judge",
    "text": 'SCORES: {"mvp-first": 6, "risk-first": 8, "architecture-first": 7}\n' + FINAL_PLAN,
}


def cfg(**extra):
    return {
        "plan_first": False,
        "proposals": False,
        "advisor_on_plan": False,
        "fanout": {"enabled": True, "max_parallel": 3, "require_tests": True, "test_command": "true"},
        **extra,
    }


async def run_job(server, command: str):
    out = await call(server, "slash.exec", {"command": command})
    await server.session.turn_task
    return out


async def test_ultraplan_three_plans_then_judge_then_file(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [*PLANNERS, JUDGE], mode="auto", autonomy=cfg())
    await call(server, "session.create", {"cwd": str(tmp_path)})
    out = await run_job(server, "ultraplan add three files")
    assert "Planning from three angles" in out["output"]
    planners = [h for h in server.subagents.handles.values()]
    assert sorted(h.description for h in planners) == [
        "plan: architecture-first",
        "plan: mvp-first",
        "plan: risk-first",
    ]
    assert all(h.agent_type == "planner" and h.tier == "strong" and h.status == "completed" for h in planners)
    calls = [c for p in server.providers for c in p.log]
    judge_i = next(
        i
        for i, c in enumerate(calls)
        if "You are the plan judge" in c["text"].split("\n")[0] or "plan judge" in c["text"]
    )
    assert sum(1 for c in calls[:judge_i] if "ANGLE:" in c["text"]) == 3  # all planners before the judge
    judge_call = calls[judge_i]
    assert all(t in judge_call["text"] for t in ("MVP-PLAN", "RISK-PLAN", "ARCH-PLAN"))
    assert judge_call["model"] == "m-strong"
    files = list((tmp_path / ".k3code" / "plans").glob("*-add-three-files.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "## Steps" in body and "STEP-TWO" in body and "risk-first: 8" in body
    art = server.artifacts.list(kind="plan")
    assert len(art) == 1 and art[0].path == str(files[0].resolve())
    shown = events(server, "plan.show")
    assert shown[-1]["source"] == "ultraplan" and shown[-1]["status"] == "proposed"
    assert len(shown[-1]["subtasks"]) == 3 and shown[-1]["fanout_candidate"] is True
    final = server.session.stored.messages[-1]["content"]
    assert "Run /go" in final and "STEP-ONE" in final
    done = events(server, "message.complete")[-1]
    assert done["status"] == "done"


async def test_ultraplan_judge_without_plan_falls_back_to_best_scored(tmp_path, monkeypatch):
    bad_judge = {
        "type": "text",
        "match": "You are the plan judge",
        "text": 'SCORES: {"mvp-first": 2, "risk-first": 9}\nno',
    }
    plans = [
        {"type": "text", "match": "ANGLE: mvp-first", "text": "## Goal\nmvp"},
        {"type": "text", "match": "ANGLE: risk-first", "text": "## Goal\nrisk wins"},
        {"type": "text", "match": "ANGLE: architecture-first", "text": "## Goal\narch"},
    ]
    server = make(tmp_path, monkeypatch, [*plans, bad_judge], mode="auto", autonomy=cfg())
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_job(server, "ultraplan thing")
    assert "risk wins" in server.session.stored.messages[-1]["content"]
    assert "used the risk-first plan" in server.session.stored.messages[-1]["content"]


async def test_ultraplan_refuses_while_a_turn_runs_and_needs_task(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], mode="auto", autonomy=cfg())
    await call(server, "session.create", {"cwd": str(tmp_path)})
    out = await call(server, "slash.exec", {"command": "ultraplan"})
    assert "Usage" in out["output"]


async def test_go_executes_ultraplan_with_fanout(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    steps = [
        *PLANNERS,
        JUDGE,
        *worker(1, "f1.txt"),
        *worker(2, "f2.txt"),
        *worker(3, "f3.txt"),
        reviewer("STEP-ONE"),
        reviewer("STEP-TWO"),
        reviewer("STEP-THREE"),
    ]
    server = make(tmp_path, monkeypatch, steps, mode="auto", autonomy=cfg())
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultraplan add three files")
    go = await call(server, "slash.exec", {"command": "go"})
    assert go["type"] == "send" and go["text"] == "add three files"
    assert server.session.preapproved_plan["plan"].startswith("## Goal")
    await call(server, "prompt.submit", {"text": go["text"]})
    await server.session.turn_task
    assert server.session.preapproved_plan is None  # consumed
    done = events(server, "fanout.done")[0]
    assert done["ok"] and done["merged"] == 3
    assert [(repo / f"f{i}.txt").exists() for i in (1, 2, 3)] == [True] * 3
    assert git(repo, "log", "--oneline", "--merges").count("Merge k3/") == 3


# ── /ultracode ──

LENS_C = {
    "type": "text",
    "match": "LENS: correctness\nAdversarially",
    "text": json.dumps(
        [
            {"file": "f1.txt", "line": 1, "severity": "high", "issue": "REAL-BUG f1 is wrong"},
            {"file": "f2.txt", "line": 1, "severity": "low", "issue": "NITPICK f2 style"},
        ]
    ),
}
LENS_S = {
    "type": "text",
    "match": "LENS: security/robustness\nAdversarially",
    "text": json.dumps([{"file": "f1.txt", "line": 1, "severity": "high", "issue": "REAL-BUG f1 is wrong"}]),
}
VOTE_C = {"type": "text", "match": "VOTE LENS: correctness", "text": json.dumps({"votes": {"f1": True, "f2": True}})}
VOTE_S = {
    "type": "text",
    "match": "VOTE LENS: security/robustness",
    "text": json.dumps({"votes": {"f1": True, "f2": False}}),
}
FIXER = [
    {
        "type": "tool_call",
        "id": "fx",
        "match": "Fix these confirmed review findings",
        "when": "first",
        "name": "write",
        "arguments": {"path": "fixed.txt", "content": "fixed f1\n"},
    },
    {"type": "text", "match": "Fix these confirmed review findings", "when": "after_tool", "text": "fixed it"},
]


def code_steps(*extra):
    return [
        *PLANNERS,
        JUDGE,
        *worker(1, "f1.txt"),
        *worker(2, "f2.txt"),
        *worker(3, "f3.txt"),
        reviewer("STEP-ONE"),
        reviewer("STEP-TWO"),
        reviewer("STEP-THREE"),
        *extra,
    ]


async def test_ultracode_accepts_only_findings_both_reviewers_agree_on(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path, monkeypatch, code_steps(LENS_C, LENS_S, VOTE_C, VOTE_S, *FIXER), mode="auto", autonomy=cfg()
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "2 candidate finding(s); 1 confirmed by both reviewers, 1 rejected" in report
    assert (
        "REAL-BUG f1 is wrong" in report and "NITPICK" not in report.split("## Final tests")[0].split("Review panel")[1]
    )
    # the fixer only got the confirmed finding
    fixer_call = next(c for p in server.providers for c in p.log if "Fix these confirmed review findings" in c["text"])
    assert "REAL-BUG" in fixer_call["text"] and "NITPICK" not in fixer_call["text"]
    assert (repo / "fixed.txt").read_text() == "fixed f1\n"  # fix merged back
    assert "Final tests" in report and "pass (true)" in report
    assert "Budget used:" in report
    for i in (1, 2, 3):
        assert (repo / f"f{i}.txt").exists()
    kinds = {a.kind for a in server.artifacts.list()}
    assert {"plan", "review"} <= kinds
    reports = list((repo / ".k3code" / "reports").glob("*-ultracode-*.md"))
    assert len(reports) == 1
    phases = [e["phase"] for e in events(server, "ultra.progress")]
    for ph in (
        "planning",
        "judging",
        "implementing",
        "adversarial review",
        "cross-checking findings",
        "fixing",
        "final tests",
    ):
        assert ph in phases, phases
    assert git(repo, "status", "--porcelain").strip() == ""


async def test_ultracode_no_agreement_means_no_fix(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    vote_c = {
        "type": "text",
        "match": "VOTE LENS: correctness",
        "text": json.dumps({"votes": {"f1": True, "f2": False}}),
    }
    vote_s = {
        "type": "text",
        "match": "VOTE LENS: security/robustness",
        "text": json.dumps({"votes": {"f1": False, "f2": True}}),
    }
    server = make(
        tmp_path, monkeypatch, code_steps(LENS_C, LENS_S, vote_c, vote_s, *FIXER), mode="auto", autonomy=cfg()
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "0 confirmed" in report and "no finding was confirmed by both reviewers" in report
    assert not (repo / "fixed.txt").exists()
    assert not any("Fix these confirmed" in c["text"] for p in server.providers for c in p.log)


async def test_ultracode_agent_budget_stops_it(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        code_steps(LENS_C, LENS_S, VOTE_C, VOTE_S, *FIXER),
        mode="auto",
        autonomy=cfg(),
        ultracode={"max_agents": 3},
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "STOPPED" in report and "agent budget exhausted (3/3 agents)" in report
    assert [h.agent_type for h in server.subagents.handles.values()].count("worker") == 0
    assert not (repo / "f1.txt").exists()
    assert len(server.subagents.handles) == 3  # only the three planners ran


async def test_ultracode_token_budget_stops_it(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server = make(
        tmp_path,
        monkeypatch,
        code_steps(LENS_C, LENS_S, VOTE_C, VOTE_S, *FIXER),
        mode="auto",
        autonomy=cfg(),
        ultracode={"max_tokens": 300},
    )
    await call(server, "session.create", {"cwd": str(repo)})
    await run_job(server, "ultracode add three files")
    report = server.session.stored.messages[-1]["content"]
    assert "STOPPED" in report and "token budget exhausted" in report
    assert not (repo / "f1.txt").exists()


async def test_a_budget_stop_cancels_the_sibling_children():
    """asyncio.gather re-raised BudgetStop and left the siblings running: their sub-agents kept spending."""
    import asyncio

    from k3code.autonomy.ultra import gather_or_cancel
    from k3code.subagents.budget import BudgetStop

    cancelled = []

    async def long_child():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def over_budget():
        await asyncio.sleep(0.01)
        raise BudgetStop("agent budget exhausted")

    try:
        await asyncio.wait_for(gather_or_cancel(long_child(), over_budget(), long_child()), 5)
        raise AssertionError("expected BudgetStop")
    except BudgetStop:
        pass
    assert cancelled == [True, True]
    assert await gather_or_cancel(asyncio.sleep(0, "a"), asyncio.sleep(0, "b")) == ["a", "b"]


def test_parsers():
    scores, plan = parse_judge('blah SCORES: {"a": 3, "b": "7"}\n## Goal\nx')
    assert scores == {"a": 3.0, "b": 7.0} and plan.startswith("## Goal")
    assert parse_judge("no scores")[0] == {}
    fs = parse_findings('Here:\n[{"file": "a", "line": 1, "issue": "x"}, {"file": "b", "issue": "y"}]\nthanks')
    assert [f["issue"] for f in fs] == ["x", "y"] and parse_findings("[]") == [] and parse_findings("nothing") == []
    d = dedupe_findings(
        [
            {"file": "a", "line": 1, "issue": "Same thing"},
            {"file": "a", "line": 1, "issue": "same thing"},
            {"file": "b", "line": 2, "issue": "other"},
        ]
    )
    assert [f["id"] for f in d] == ["f1", "f2"]
    assert parse_votes('{"votes": {"f1": true, "f2": false}}') == {"f1": True, "f2": False}
    assert parse_votes('{"f1": true}') == {"f1": True} and parse_votes("garbage") == {}
