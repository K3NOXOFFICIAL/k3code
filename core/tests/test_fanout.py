"""Fan-out executor: parallel worktree children, reviewer gate, merge + tests, retry once, escalation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from k3code.autonomy.fanout import detect_test_command, extract_subtasks, parse_verdict, run_tests
from k3code.autonomy.scope import ScopeVerdict
from m1cmd_helpers import git_repo
from test_autonomy_gateway import PLAN, call, events, make, run_turn


def classify(subs: list[str]) -> dict:
    return {
        "type": "text",
        "match": "You classify a coding task",
        "text": json.dumps(
            {
                "scope": "large",
                "needs_plan": True,
                "risk": "low",
                "parallelizable": True,
                "suggested_subtasks": subs,
                "reason": "big",
            }
        ),
    }


PLANNER = [
    {"type": "tool_call", "match": "PLANNING mode", "when": "first", "name": "exit_plan", "arguments": {"plan": PLAN}}
]


def worker(i: int, name: str, *, sleep: float = 0.0, content: str = "ok\n") -> list[dict]:
    key = f"YOUR subtask (t{i})"
    steps: list[dict] = []
    if sleep:
        steps.append(
            {
                "type": "tool_call",
                "id": "s",
                "match": key,
                "when": "first",
                "name": "bash",
                "arguments": {"command": f"sleep {sleep}"},
            }
        )
    steps += [
        {
            "type": "tool_call",
            "id": "w",
            "match": key,
            "when": "first",
            "name": "write",
            "arguments": {"path": name, "content": content},
        },
        {"type": "text", "match": key, "when": "after_tool", "text": f"worker {i} done"},
    ]
    return steps


def reviewer(title: str, verdict: str = "pass", *, after: str = "") -> dict:
    s = {"type": "text", "match": f"Subtask: {title}", "text": f"review of {title}. VERDICT: {verdict}"}
    return s


def make_fan(tmp: Path, monkeypatch, steps: list[dict], repo: Path, **fan):
    cfg = {
        "plan_first": True,
        "proposals": False,
        "advisor_on_plan": False,
        "fanout": {"enabled": True, "max_parallel": 3, "require_tests": True, **fan},
    }
    server = make(tmp, monkeypatch, steps, mode="auto", autonomy=cfg)
    return server


async def start_in(server, repo: Path):
    await call(server, "session.create", {"cwd": str(repo)})


def concurrency(server, agent_type: str | None = None) -> int:
    live = peak = 0
    for line in server._frames:
        f = json.loads(line)
        if f.get("method") != "event":
            continue
        t, p = f["params"]["type"], f["params"]["payload"]
        if agent_type and p.get("agent_type") != agent_type:
            continue
        if t == "subagent.start":
            live += 1
            peak = max(peak, live)
        elif t == "subagent.complete":
            live -= 1
    return peak


def git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout


SUBS = ["SUB-ONE add f1", "SUB-TWO add f2", "SUB-THREE add f3"]


def steps_for(
    subs: list[str], extra: list[dict] | None = None, reviews: list[dict] | None = None, sleep: float = 0.15
) -> list[dict]:
    steps = [classify(subs), *PLANNER]
    for i, _ in enumerate(subs, 1):
        steps += worker(i, f"f{i}.txt", sleep=sleep)
    steps += reviews or [reviewer(t) for t in subs]
    steps += extra or []
    return steps


async def test_fanout_runs_children_in_parallel_reviews_merges_and_tests_each_merge(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    # the test command runs sandboxed: it may write only inside the project, and .k3code/ is git-ignored
    log = repo / ".k3code" / "testlog.txt"
    server = make_fan(tmp_path, monkeypatch, steps_for(SUBS), repo, test_command=f"echo ran >> {log}")
    await start_in(server, repo)
    await run_turn(server, "build the whole feature")
    plan = events(server, "fanout.plan")[0]
    assert plan["max_parallel"] == 3 and [s["title"] for s in plan["subtasks"]] == SUBS
    assert plan["test_command"].startswith("echo ran")
    done = events(server, "fanout.done")[0]
    assert done["ok"] and done["merged"] == 3 and done["tests"] == "pass"
    assert concurrency(server, "worker") == 3  # all three workers overlapped
    assert log.read_text().count("ran") == 3  # tests ran once per merge
    for i in (1, 2, 3):
        assert (repo / f"f{i}.txt").read_text() == "ok\n"
    prog = events(server, "fanout.progress")
    assert {p["state"] for p in prog} >= {"running", "reviewing", "merging", "testing", "merged"}
    assert prog[-1]["done"] == 3
    reviewers = [h for h in server.subagents.handles.values() if h.agent_type == "reviewer"]
    assert len(reviewers) == 3 and all(h.tier == "strong" for h in reviewers)
    graph = git(repo, "log", "--oneline", "--merges")
    assert graph.count("Merge k3/") == 3
    assert "k3/" not in git(repo, "branch")  # merged branches cleaned up
    assert server.session.stored.messages[-1]["content"].startswith("Fan-out finished: 3/3")
    assert git(repo, "status", "--porcelain").strip() == ""


async def test_fanout_cap_limits_parallelism(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-A one", "SUB-B two", "SUB-C three", "SUB-D four"]
    server = make_fan(tmp_path, monkeypatch, steps_for(subs), repo, max_parallel=2, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    assert events(server, "fanout.plan")[0]["max_parallel"] == 2
    assert concurrency(server) == 2  # workers + reviewers together never exceed the cap
    assert events(server, "fanout.done")[0]["merged"] == 4


async def test_fanout_io_heavy_subtasks_capped_at_two(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-A install deps", "SUB-B add docs", "SUB-C more docs"]
    server = make_fan(tmp_path, monkeypatch, steps_for(subs), repo, max_parallel=3, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    plan = events(server, "fanout.plan")[0]
    assert plan["io_heavy"] is True and plan["max_parallel"] == 2
    assert concurrency(server) <= 2


async def test_reviewer_rejection_sends_work_back_once(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE add f1", "SUB-TWO add f2"]
    rework = [
        # a file is read before it is rewritten (#100: write refuses one this loop has not read)
        {
            "type": "tool_call",
            "id": "r2",
            "match": "REWORK",
            "when": "first",
            "name": "read",
            "arguments": {"path": "f2.txt"},
        },
        {
            "type": "tool_call",
            "id": "w2",
            "match": "REWORK",
            "when": "first",
            "name": "write",
            "arguments": {"path": "f2.txt", "content": "better\n"},
        },
        {"type": "text", "match": "REWORK", "when": "after_tool", "text": "worker 2 fixed"},
    ]
    reviews = [
        reviewer("SUB-ONE add f1"),
        {"type": "text", "match": "Subtask: SUB-TWO add f2", "text": "no tests added. VERDICT: fail"},
    ]
    # after the rework the reviewer sees "worker 2 fixed" in the worker report and passes it
    steps = [
        classify(subs),
        *PLANNER,
        *worker(1, "f1.txt"),
        *worker(2, "f2.txt"),
        *rework,
        *reviews,
        {"type": "text", "match": "worker 2 fixed", "text": "now fine. VERDICT: pass"},
    ]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert done["ok"], done["summary"]
    assert (repo / "f2.txt").read_text() == "better\n"
    states = [p["state"] for p in events(server, "fanout.progress") if p["subtask_id"] == "t2"]
    assert "retrying" in states and states[-1] == "merged"


async def test_a_reviewer_without_a_verdict_escalates_instead_of_passing(tmp_path, monkeypatch):
    # issue #97: a review with no VERDICT line (a crash, a stop, a ramble) used to count as a pass and was merged
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE add f1", "SUB-TWO add f2"]
    steps = [
        classify(subs),
        *PLANNER,
        *worker(1, "f1.txt"),
        *worker(2, "f2.txt"),
        reviewer(subs[0]),
        {"type": "text", "match": "Subtask: SUB-TWO add f2", "text": "I ran out of time before deciding."},
        {"type": "text", "match": "needs you (escalated)", "text": "parent handled it"},
    ]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 1 and done["escalated"] == 1, done["summary"]
    assert (repo / "f1.txt").exists() and not (repo / "f2.txt").exists()  # the unreviewed work was not merged
    t2 = [p for p in events(server, "fanout.progress") if p["subtask_id"] == "t2"]
    assert t2[-1]["state"] == "escalated" and "no review verdict" in t2[-1]["detail"]
    assert not any("--- REWORK ---" in c["text"] for p in server.providers for c in p.log)  # not the worker's fault
    reviewers = [h for h in server.subagents.handles.values() if h.agent_type == "reviewer"]
    assert len(reviewers) == 3  # SUB-ONE once, SUB-TWO asked once more
    assert "k3/" in git(repo, "branch")  # the escalated child's branch survives


async def test_failing_tests_are_sent_back_once_then_pass(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE add f1", "SUB-TWO add bad"]
    steps = [
        classify(subs),
        *PLANNER,
        *worker(1, "f1.txt"),
        *worker(2, "bad.txt"),
        reviewer(subs[0]),
        reviewer(subs[1]),
        {
            "type": "tool_call",
            "id": "rm",
            "match": "tests failed after merging",
            "when": "first",
            "name": "bash",
            "arguments": {"command": "rm bad.txt && echo fixed > f2.txt"},
        },
        {"type": "text", "match": "tests failed after merging", "when": "after_tool", "text": "worker 2 fixed"},
    ]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="test ! -f bad.txt")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert done["ok"] and done["tests"] == "pass", done["summary"]
    assert not (repo / "bad.txt").exists() and (repo / "f2.txt").exists()
    t2 = [p for p in events(server, "fanout.progress") if p["subtask_id"] == "t2"]
    assert any(p["state"] == "retrying" and "tests failed" in p["detail"] for p in t2)
    assert git(repo, "log", "--oneline", "--merges").count("Merge k3/") == 2


async def test_work_reworked_after_failing_tests_is_reviewed_again_before_it_merges(tmp_path, monkeypatch):
    # the rework after a failed test run (or a conflict) went straight back to the merge: what it added was never
    # reviewed
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE add f1", "SUB-TWO add bad"]
    steps = [
        classify(subs),
        *PLANNER,
        *worker(1, "f1.txt"),
        *worker(2, "bad.txt"),
        reviewer(subs[0]),
        reviewer(subs[1]),
        {
            "type": "tool_call",
            "id": "rm",
            "match": "--- REWORK ---",
            "when": "first",
            "name": "bash",
            "arguments": {"command": "rm bad.txt && echo unreviewed > unreviewed.txt"},
        },
        {"type": "text", "match": "--- REWORK ---", "when": "after_tool", "text": "REWORK-REPORT done"},
        # the second review of SUB-TWO carries the rework's report and rejects it
        {"type": "text", "match": "REWORK-REPORT", "text": "\nunreviewed.txt is unrelated. VERDICT: fail"},
        {"type": "text", "match": "needs you (escalated)", "text": "parent handled it"},
    ]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="test ! -f bad.txt")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 1 and done["escalated"] == 1, done["summary"]
    assert not (repo / "unreviewed.txt").exists() and "unreviewed.txt" not in git(repo, "ls-files")
    t2_reviews = [h for h in server.subagents.handles.values() if h.description == "review: SUB-TWO add bad"]
    assert len(t2_reviews) == 2
    t2 = [p for p in events(server, "fanout.progress") if p["subtask_id"] == "t2"]
    assert t2[-1]["state"] == "escalated" and "unreviewed.txt is unrelated" in t2[-1]["detail"]
    assert "k3/" in git(repo, "branch")  # the escalated child's branch survives


async def test_persistent_failure_escalates_to_parent_and_keeps_branch(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE add f1", "SUB-TWO add bad"]
    steps = [
        classify(subs),
        *PLANNER,
        *worker(1, "f1.txt"),
        *worker(2, "bad.txt"),
        reviewer(subs[0]),
        reviewer(subs[1]),
        # the retry does not fix anything
        {"type": "text", "match": "--- REWORK ---", "text": "I could not fix it"},
        {"type": "text", "match": "needs you (escalated)", "text": "parent handled it"},
    ]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="test ! -f bad.txt")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 1 and done["escalated"] == 1 and done["tests"] == "fail"
    assert (repo / "f1.txt").exists() and not (repo / "bad.txt").exists()  # the bad merge was not committed
    branches = git(repo, "branch")
    assert "k3/" in branches  # the escalated child's branch survives
    final = server.session.stored.messages[-1]["content"]
    assert final == "parent handled it"  # the parent model ran with the escalation prompt
    parent_call = [c for p in server.providers for c in p.log if "needs you (escalated)" in c["text"]]
    assert parent_call and "tests failed after merging" in parent_call[0]["text"]
    assert git(repo, "status", "--porcelain").strip() == ""


async def test_not_a_git_repo_runs_normally(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    steps = [classify(SUBS), *PLANNER, {"type": "text", "match": "Approved plan", "text": "did it sequentially"}]
    server = make_fan(tmp_path, monkeypatch, steps, plain, test_command="true")
    await start_in(server, plain)
    await run_turn(server, "do the big thing")
    assert not events(server, "fanout.plan")
    assert server.session.stored.messages[-1]["content"] == "did it sequentially"


@pytest.mark.parametrize("staged", [True, False])
async def test_uncommitted_changes_skip_fanout_and_run_normally(tmp_path, monkeypatch, staged):
    """#98: workers branch from HEAD and merge into the user's checkout. With a staged edit git refuses every merge
    (with an unstaged one, every merge touching that file), after all the workers and a rework each were paid for."""
    repo = git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 2\n")
    if staged:
        git(repo, "add", "a.py")
    steps = [classify(SUBS), *PLANNER, {"type": "text", "match": "Approved plan", "text": "did it sequentially"}]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    assert not events(server, "fanout.plan")
    assert not [h for h in server.subagents.handles.values() if h.agent_type == "worker"]
    notes = [n["text"] for n in events(server, "notification.show") if n.get("kind") == "fanout"]
    assert notes and "uncommitted changes" in notes[0] and "a.py" in notes[0], notes
    assert server.session.stored.messages[-1]["content"] == "did it sequentially"
    assert git(repo, "status", "--porcelain").startswith("M " if staged else " M")  # the user's edit is untouched


async def test_checkout_dirtied_during_fanout_is_not_a_conflict_and_not_reworked(tmp_path, monkeypatch):
    """#98: a merge git refuses to start (the checkout's own changes are in the way) was reported as a 'merge
    conflict with what was already merged' and sent back to the worker, who cannot fix the user's checkout."""
    from k3code.subagents import worktree as wt_mod

    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE add f1", "SUB-TWO add f2"]
    steps = [
        *steps_for(subs, sleep=0),
        {"type": "text", "match": "--- REWORK ---", "text": "rework ran"},
        {"type": "text", "match": "needs you (escalated)", "text": "parent handled it"},
    ]
    real_trial = wt_mod.merge_trial

    async def user_stages_an_edit_first(tree):
        (repo / "a.py").write_text("x = 2\n")
        git(repo, "add", "a.py")  # the user stages an edit while the workers run
        return await real_trial(tree)

    monkeypatch.setattr(wt_mod, "merge_trial", user_stages_an_edit_first)
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 0 and done["escalated"] == 2, done["summary"]
    prog = events(server, "fanout.progress")
    assert "retrying" not in {p["state"] for p in prog}  # no rework agent spent on the checkout's problem
    assert len([h for h in server.subagents.handles.values() if h.agent_type == "worker"]) == 2
    details = [p["detail"] for p in prog if p["state"] == "escalated"]
    assert all("merge conflict" not in d and "uncommitted changes" in d and "a.py" in d for d in details), details
    assert git(repo, "diff", "--cached", "--name-only").split() == ["a.py"]  # the user's staged edit is untouched
    assert "k3/" in git(repo, "branch")  # the work stays on its branch for the parent
    assert server.session.stored.messages[-1]["content"] == "parent handled it"


async def test_fanout_disabled_by_config(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    steps = [classify(SUBS), *PLANNER, {"type": "text", "match": "Approved plan", "text": "sequential"}]
    server = make_fan(tmp_path, monkeypatch, steps, repo, enabled=False)
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    assert not events(server, "fanout.plan")
    assert server.session.stored.messages[-1]["content"] == "sequential"


# ── the auto fan-out's budget (issue #136): the gateway passed none, so children ran without an agent/token cap ──

#: the parent takes over the subtasks a budget stop left (escalation_prompt names them ``needs you (skipped)``)
PARENT_TAKES_OVER = {"type": "text", "match": "needs you (skipped)", "text": "parent handled it"}


def starts(server, agent_type: str | None = None) -> int:
    return sum(1 for p in events(server, "subagent.start") if agent_type in (None, p.get("agent_type")))


async def test_auto_fanout_stops_starting_children_at_max_agents(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    # one at a time, so the stop point is exact: t1's worker and reviewer are the two agents, t2 and t3 never start
    steps = steps_for(SUBS, extra=[PARENT_TAKES_OVER], sleep=0)
    server = make_fan(tmp_path, monkeypatch, steps, repo, max_parallel=1, max_agents=2, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "build the whole feature")
    assert starts(server) == 2
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 1 and done["escalated"] == 2
    assert "Stopped early: agent budget exhausted (2/2 agents)" in done["summary"]
    skipped = [p for p in events(server, "fanout.progress") if p["state"] == "skipped"]
    assert {p["subtask_id"] for p in skipped} == {"t2", "t3"}
    assert all("agent budget exhausted" in p["detail"] for p in skipped)
    assert (repo / "f1.txt").exists() and not (repo / "f2.txt").exists()
    assert server.session.stored.messages[-1]["content"] == "parent handled it"


async def test_auto_fanout_keeps_a_reviewer_slot_for_every_worker(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    # 3 agents: t1's worker + reviewer use two; t2's worker would start with one slot left and have no reviewer
    steps = steps_for(SUBS, extra=[PARENT_TAKES_OVER], sleep=0)
    server = make_fan(tmp_path, monkeypatch, steps, repo, max_parallel=1, max_agents=3, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "build the whole feature")
    assert starts(server) == 2 and starts(server, "worker") == 1
    done = events(server, "fanout.done")[0]
    assert done["merged"] == 1
    assert "Stopped early: agent budget exhausted (2/3 agents)" in done["summary"]
    skipped = [p for p in events(server, "fanout.progress") if p["state"] == "skipped"]
    assert {p["subtask_id"] for p in skipped} == {"t2", "t3"}


async def test_auto_fanout_keeps_a_reviewer_slot_for_every_parallel_worker(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    # 3 workers at once with 4 agents: a third worker would leave one slot for two finished workers, whose results
    # would be discarded unreviewed. Two workers start, and each keeps its reviewer slot.
    steps = steps_for(SUBS, extra=[PARENT_TAKES_OVER], sleep=0)
    server = make_fan(tmp_path, monkeypatch, steps, repo, max_parallel=3, max_agents=4, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "build the whole feature")
    assert starts(server, "worker") == 2 and starts(server, "reviewer") == 2
    done = events(server, "fanout.done")[0]
    assert done["merged"] == 2 and done["escalated"] == 1
    assert "Stopped early: agent budget exhausted (2/4 agents, 2 kept for reviewers)" in done["summary"]
    skipped = [p for p in events(server, "fanout.progress") if p["state"] == "skipped"]
    assert {p["subtask_id"] for p in skipped} == {"t3"}
    assert (repo / "f1.txt").exists() and (repo / "f2.txt").exists() and not (repo / "f3.txt").exists()


async def test_auto_fanout_token_cap_stops_it(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    # t1's worker would spend 2 calls x 350 tokens; with a 300-token cap it is stopped after its first call (it used
    # to run to the end, 700 tokens), and its reviewer and every later child is refused
    usage = {"type": "usage", "match": "YOUR subtask (t1)", "prompt_tokens": 200, "completion_tokens": 150}
    steps = steps_for(SUBS, extra=[usage, PARENT_TAKES_OVER], sleep=0)
    server = make_fan(tmp_path, monkeypatch, steps, repo, max_parallel=1, max_tokens=300, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "build the whole feature")
    assert starts(server) == 1 and starts(server, "reviewer") == 0
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 0
    assert "Stopped early: token budget exhausted (350/300 tokens); stopped while running" in done["summary"]
    assert f"- [stopped] {SUBS[0]} — token budget exhausted (350/300 tokens)" in done["summary"]
    assert not (repo / "f1.txt").exists()  # the unreviewed work was not merged
    assert server.session.stored.messages[-1]["content"] == "parent handled it"


async def test_fanout_stops_starting_children_once_the_session_budget_is_spent(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    # no fan-out cap: the parent's reliability.session_tokens (children are charged to it) is what stops it. Each of
    # t1's two children stays inside its own 300-token session budget; together they spend 400 of the parent's
    spend = [
        {
            "type": "usage",
            "match": "YOUR subtask (t1)",
            "when": "after_tool",
            "prompt_tokens": 150,
            "completion_tokens": 50,
        },
        {"type": "usage", "match": f"Subtask: {SUBS[0]}", "prompt_tokens": 150, "completion_tokens": 50},
    ]
    steps = steps_for(SUBS, extra=[*spend, PARENT_TAKES_OVER], sleep=0)
    autonomy = {
        "plan_first": True,
        "proposals": False,
        "advisor_on_plan": False,
        "fanout": {"enabled": True, "max_parallel": 1, "max_tokens": 0, "test_command": "true"},
    }
    server = make(tmp_path, monkeypatch, steps, mode="auto", autonomy=autonomy, reliability={"session_tokens": 300})
    await start_in(server, repo)
    await run_turn(server, "build the whole feature")
    assert starts(server) == 2  # t1's worker and reviewer; t2's worker is refused
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 1
    assert "Stopped early: session token budget exceeded: 400 > 300" in done["summary"]


def test_fanout_budget_defaults_and_derivation():
    from types import SimpleNamespace

    from k3code.autonomy.fanout import FanoutExecutor

    def budget(n: int, **fan):
        return FanoutExecutor(SimpleNamespace(config=SimpleNamespace(autonomy={"fanout": fan}))).budget(n)

    assert (budget(3).max_agents, budget(3).max_tokens) == (12, 1_000_000)  # 4 children per subtask
    assert budget(8).max_agents == 16  # capped
    assert budget(8, max_agents=5).max_agents == 5
    assert budget(2, max_tokens=0).max_tokens is None  # 0 = no token cap
    # anything that is not a non-negative whole number falls back to the default (4 agents per subtask, 1,000,000
    # tokens): a typo must not remove the cap; 0 is the one way to say "no token cap"
    assert budget(2, max_tokens=1e6).max_tokens == 1_000_000  # a whole float is a whole number
    assert budget(2, max_tokens=250).max_tokens == 250
    for bad in (-5, -1, None, "lots", "500k", "1e6", "", True, False, 1.5, float("nan"), float("inf"), [], {}):
        b = budget(3, max_agents=bad, max_tokens=bad)
        assert (b.max_agents, b.max_tokens) == (12, 1_000_000), bad


def test_extract_subtasks_and_verdict_parsing():
    v = ScopeVerdict(scope="large", parallelizable=True, suggested_subtasks=["a", "b"])
    assert extract_subtasks(v, "") == ["a", "b"]
    v = ScopeVerdict(scope="large", parallelizable=True)
    assert extract_subtasks(v, "## Steps\n1. do x\n2. do y\n3) do z\n## Files\na.py") == ["do x", "do y", "do z"]
    assert extract_subtasks(ScopeVerdict(scope="large", parallelizable=False), "## Steps\n1. x\n2. y") == []
    assert extract_subtasks(ScopeVerdict(scope="large", parallelizable=True, suggested_subtasks=["only"]), "") == []
    assert parse_verdict("meh VERDICT: fail\nlater VERDICT: pass") is True
    assert parse_verdict("VERDICT: FAIL") is False and parse_verdict("no verdict") is None


def test_detect_test_command(tmp_path):
    assert detect_test_command(tmp_path) is None
    (tmp_path / "Cargo.toml").write_text("")
    assert detect_test_command(tmp_path) == "cargo test"
    (tmp_path / "go.mod").write_text("")
    assert detect_test_command(tmp_path) == "go test ./..."
    (tmp_path / "pytest.ini").write_text("")
    assert detect_test_command(tmp_path) == "python3 -m pytest -q -x"  # stock Ubuntu has no `python`
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert detect_test_command(tmp_path) == "npm test --silent"
    (tmp_path / "package.json").write_text('{"scripts": {}}')
    assert detect_test_command(tmp_path) == "python3 -m pytest -q -x"
    fake_python(tmp_path, has_pytest=True)
    assert detect_test_command(tmp_path) == ".venv/bin/python -m pytest -q -x"


def fake_python(root: Path, *, has_pytest: bool) -> None:
    """A stand-in ``.venv/bin/python``: ``-m pytest`` passes; ``import pytest`` works only when ``has_pytest``."""
    py = root / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    missing = "echo \"ModuleNotFoundError: No module named 'pytest'\" >&2; exit 1"
    py.write_text(f'#!/bin/sh\ncase "$*" in *"import pytest"*) {"exit 0" if has_pytest else missing};; esac\nexit 0\n')
    py.chmod(0o755)


async def test_detected_pytest_uses_the_project_venv_inside_the_sandbox(tmp_path):
    """Issue #96: `python -m pytest` exited 127 in the sandbox (no `python`, the venv not on PATH)."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    fake_python(project, has_pytest=True)
    cmd = detect_test_command(project)
    assert cmd == ".venv/bin/python -m pytest -q -x"
    assert await run_tests(cmd, project, 60) == (True, "")


async def test_fanout_skipped_when_the_detected_test_runner_is_missing_in_the_sandbox(tmp_path, monkeypatch):
    """Issue #96: every merge would fail its tests, rework and escalate; the parent runs the task instead."""
    repo = git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text("")
    fake_python(repo, has_pytest=False)
    steps = [classify(SUBS), *PLANNER, {"type": "text", "match": "Approved plan", "text": "did it sequentially"}]
    server = make_fan(tmp_path, monkeypatch, steps, repo)
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    assert not events(server, "fanout.plan")
    notes = [n["text"] for n in events(server, "notification.show") if n.get("kind") == "fanout"]
    assert len(notes) == 1 and notes[0].startswith("fan-out skipped")
    assert ".venv/bin/python -m pytest" in notes[0] and "No module named 'pytest'" in notes[0]
    assert server.session.stored.messages[-1]["content"] == "did it sequentially"


async def test_fanout_skipped_when_the_sandbox_is_unusable(tmp_path, monkeypatch):
    """Issue #96: without bwrap every child's bash and every merge's tests are refused."""
    from k3code.reliability import sandbox

    repo = git_repo(tmp_path / "repo")
    steps = [classify(SUBS), *PLANNER, {"type": "text", "match": "Approved plan", "text": "did it sequentially"}]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="true")
    monkeypatch.setattr(sandbox, "usable", lambda: False)
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    assert not events(server, "fanout.plan")
    notes = [n["text"] for n in events(server, "notification.show") if n.get("kind") == "fanout"]
    assert len(notes) == 1 and "bubblewrap is unusable" in notes[0]
    assert server.session.stored.messages[-1]["content"] == "did it sequentially"


async def test_merge_conflict_between_children_is_resolved_by_the_child(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE shared A", "SUB-TWO shared B"]
    steps = [
        classify(subs),
        *PLANNER,
        *worker(1, "shared.txt", content="from one\n"),
        *worker(2, "shared.txt", content="from two\n"),
        reviewer(subs[0]),
        reviewer(subs[1]),
        # the child reads the conflicted file before it writes the resolution (#100)
        {
            "type": "tool_call",
            "id": "see",
            "match": "conflicts in",
            "when": "first",
            "name": "read",
            "arguments": {"path": "shared.txt"},
        },
        {
            "type": "tool_call",
            "id": "fix",
            "match": "conflicts in",
            "when": "first",
            "name": "write",
            "arguments": {"path": "shared.txt", "content": "from one\nfrom two\n"},
        },
        {"type": "text", "match": "conflicts in", "when": "after_tool", "text": "worker resolved"},
    ]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert done["ok"], done["summary"]
    assert (repo / "shared.txt").read_text() == "from one\nfrom two\n"
    states = [p["state"] for p in events(server, "fanout.progress") if p["state"] == "retrying"]
    assert len(states) == 1  # exactly one subtask was sent back
    assert git(repo, "status", "--porcelain").strip() == ""


def test_active_rows_include_running_children():
    from types import SimpleNamespace

    from k3code.gateway.server import GatewayServer
    from k3code.subagents.runner import Handle

    h = Handle(
        id="sa-1", description="do x", agent_type="worker", tier="cheap", depth=1, parent_sid="p", status="running"
    )
    fake = SimpleNamespace(live={}, subagents=SimpleNamespace(handles={"sa-1": h}))
    rows = GatewayServer._active_rows(fake, None)
    assert rows[0]["id"] == "sa-1" and rows[0]["origin"] == "subagent" and rows[0]["background"]


# ── the token cap holds for children that are already running (follow-up to #136) ──


@pytest.mark.serial  # two children's calls have to interleave
async def test_token_cap_stops_children_that_are_already_running(tmp_path, monkeypatch):
    """The cap was checked only before a child started and charged only after it ended, so running children were
    never stopped: up to max_parallel of them overshot it by a whole run each. Two parallel workers that would each
    make 20 calls of 400 tokens are now stopped once the cap is crossed, and their subtasks say why."""
    import asyncio
    import re

    from k3code.providers.fake import FakeProvider
    from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
    from k3code.reliability import sandbox

    monkeypatch.setattr(sandbox, "usable", lambda: True)  # nothing here runs in it: no bash, nothing is merged
    real_stream = FakeProvider.stream
    calls: dict[str, int] = {}

    async def busy_workers(self, messages, tools, model, **kw):
        found = re.search(r"YOUR subtask \((t\d)\)", "\n".join(str(m.content or "") for m in messages))
        if found is None:  # the classifier, the planner and the parent: the script answers them
            async for ev in real_stream(self, messages, tools, model, **kw):
                yield ev
            return
        key = found.group(1)
        n = calls[key] = calls.get(key, 0) + 1
        await asyncio.sleep(0.02)  # the model "thinks": the two workers' calls interleave
        # a new file every call (no repetition for the loop guard to stop), done after the 20th
        todo = [ToolCall(id=f"w{n}", name="write", arguments={"path": f"{key}-{n}.txt", "content": "x\n"})]
        msg = Message(
            role="assistant",
            content=None if n < 20 else "done",
            tool_calls=todo if n < 20 else [],
            usage=Usage(prompt_tokens=300, completion_tokens=100),
        )
        yield StreamEvent(type="done", message=msg, usage=msg.usage)

    monkeypatch.setattr(FakeProvider, "stream", busy_workers)
    repo = git_repo(tmp_path / "repo")
    subs = SUBS[:2]
    steps = [classify(subs), *PLANNER, {"type": "text", "match": "needs you (stopped)", "text": "parent handled it"}]
    server = make_fan(tmp_path, monkeypatch, steps, repo, max_parallel=2, max_tokens=1000, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "build the whole feature")

    workers = [h for h in server.subagents.handles.values() if h.agent_type == "worker"]
    assert len(workers) == 2 and all(h.status == "interrupted" for h in workers)
    spent = sum(h.tokens_in + h.tokens_out for h in workers)
    assert 1000 <= spent <= 1000 + 2 * 400  # at most the call in flight of each running child; it used to be 16000
    assert max(calls.values()) < 20
    assert starts(server, "reviewer") == 0
    for t in ("t1", "t2"):
        last = [p for p in events(server, "fanout.progress") if p["subtask_id"] == t][-1]
        assert last["state"] == "stopped", last
        assert "token budget exhausted" in last["detail"] and "stopped while running" in last["detail"]
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 0 and done["escalated"] == 2
    assert "Stopped early: token budget exhausted (" in done["summary"]
    assert f"- [stopped] {subs[0]} — token budget exhausted" in done["summary"]
    assert server.session.stored.messages[-1]["content"] == "parent handled it"  # the parent was told and took over
    assert git(repo, "status", "--porcelain").strip() == ""  # nothing was merged


def test_agent_budget_counts_running_children():
    from types import SimpleNamespace

    from k3code.subagents.budget import AgentBudget, BudgetStop

    b = AgentBudget(max_tokens=1000)
    done = SimpleNamespace(id="a", tokens_in=300, tokens_out=100)
    running = SimpleNamespace(id="b", tokens_in=0, tokens_out=0)
    b.track(done)
    b.track(running)
    b.charge(done)
    assert (b.tokens, b.used, b.over()) == (400, 400, "")
    running.tokens_in, running.tokens_out = 500, 150  # its calls so far, before it ends
    assert b.used == 1050 and b.over() == "token budget exhausted (1050/1000 tokens)"
    with pytest.raises(BudgetStop):
        b.check()  # no further child starts while running ones have spent the cap
    b.charge(running)
    assert (b.tokens, b.running) == (1050, {})
