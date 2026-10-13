"""Fan-out executor: parallel worktree children, reviewer gate, merge + tests, retry once, escalation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from env_fixtures import sandbox_optional  # noqa: F401  (a fixture)
from k3code.autonomy.fanout import detect_test_command, extract_subtasks, parse_verdict
from k3code.autonomy.scope import ScopeVerdict
from m1cmd_helpers import git_repo
from test_autonomy_gateway import PLAN, call, events, make, run_turn

pytestmark = pytest.mark.usefixtures("sandbox_optional")


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
    # the test command runs sandboxed: it may write only inside the project, and .k3code/plans/ is git-ignored
    log = repo / ".k3code" / "plans" / "testlog.txt"
    log.parent.mkdir(parents=True)
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


async def test_a_fanout_that_starts_on_a_loaded_machine_runs_one_child_fewer(tmp_path, monkeypatch):
    psi = tmp_path / "pressure"
    psi.mkdir()
    (psi / "cpu").write_text("some avg10=90.00 avg60=50.00 avg300=20.00 total=1\n")
    monkeypatch.setenv("K3CODE_PSI_PATH", str(psi))
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-A one", "SUB-B two", "SUB-C three", "SUB-D four"]
    server = make_fan(tmp_path, monkeypatch, steps_for(subs), repo, max_parallel=3, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    plan = events(server, "fanout.plan")[0]
    assert plan["max_parallel"] == 2 and plan["pressure_shrink"] == 1
    assert concurrency(server) <= 2
    assert events(server, "fanout.done")[0]["merged"] == 4  # slower, not smaller


async def test_a_width_of_one_is_not_shrunk_to_zero(tmp_path, monkeypatch):
    psi = tmp_path / "pressure"
    psi.mkdir()
    (psi / "cpu").write_text("some avg10=90.00 avg60=50.00 avg300=20.00 total=1\n")
    monkeypatch.setenv("K3CODE_PSI_PATH", str(psi))
    repo = git_repo(tmp_path / "repo")
    server = make_fan(tmp_path, monkeypatch, steps_for(["SUB-A one", "SUB-B two"]), repo, max_parallel=1)
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    plan = events(server, "fanout.plan")[0]
    assert plan["max_parallel"] == 1 and plan["pressure_shrink"] == 0


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
    assert detect_test_command(tmp_path) == "python -m pytest -q -x"
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert detect_test_command(tmp_path) == "npm test --silent"
    (tmp_path / "package.json").write_text('{"scripts": {}}')
    assert detect_test_command(tmp_path) == "python -m pytest -q -x"


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


# ── plan-first fan-out: which subtasks a plan gives (dynamic sizing on, and the older reading when it is off) ──


def _applies(plan_steps: str, suggestions: list[str], *, dynamic: bool = True):
    from types import SimpleNamespace

    from k3code.autonomy.fanout import FanoutExecutor
    from k3code.autonomy.scope import ScopeVerdict
    from k3code.config import Settings

    server = SimpleNamespace(config=Settings(autonomy={"agents": {"dynamic": dynamic}}))
    verdict = ScopeVerdict(
        scope="large", parallelizable=True, fanout_candidate=True, suggested_subtasks=suggestions, source="classifier"
    )
    plan = (
        f"## Goal\nx\n## Steps\n{plan_steps}\n## Files\nnone\n## Risks\nnone\n## Verification\nrun\n## Estimate\nlarge"
    )
    gate = SimpleNamespace(verdict=verdict, plan=plan)
    return FanoutExecutor(server).applies(SimpleNamespace(background=False, reliability=None), gate)


SUGGESTED = ["edit cli.py", "document cli.py"]


def test_dependent_steps_are_not_fanned_out_by_falling_back_to_the_classifiers_guess():
    steps = "1. edit core/cli.py\n2. document core/cli.py\n3. test core/cli.py"
    assert _applies(steps, SUGGESTED) == []  # one unit: a single piece of work
    assert _applies(steps, SUGGESTED, dynamic=False) == SUGGESTED  # the older reading is unchanged


def test_a_step_that_says_after_step_n_is_one_unit_with_it():
    assert _applies("1. create the schema\n2. migrate the rows after step 1", SUGGESTED) == []


def test_only_top_level_steps_become_subtasks():
    steps = "1. add the parser\n   - handle commas\n   - handle quotes\n2. add the writer\n   - flush on close\n"
    assert _applies(steps, []) == ["add the parser", "add the writer"]
    older = _applies(steps, [], dynamic=False)
    assert len(older) == 5  # the sub-bullets were read as steps


def test_a_plan_with_fewer_than_two_steps_leaves_the_classifiers_suggestions_to_decide():
    assert _applies("1. do all of it", SUGGESTED) == SUGGESTED
    assert _applies("just prose, no list", SUGGESTED) == SUGGESTED


def test_the_plans_steps_beat_the_classifiers_suggestions_when_there_are_two_independent_ones():
    assert _applies("1. build the api [files: api/]\n2. build the ui [files: ui/]", SUGGESTED) == [
        "build the api",
        "build the ui",
    ]


async def test_a_reviewer_that_died_never_counts_as_a_pass():
    """A reviewer stopped by the ultracode token cap said nothing: the worker's result was merged as if it passed (and a
    reviewer that had written `VERDICT: fail` before the stop was lost)."""
    from types import SimpleNamespace

    from k3code.autonomy.fanout import FanoutExecutor, ReviewUnavailable, Subtask
    from k3code.subagents.budget import CAP_ERROR, AgentBudget, BudgetStop
    from k3code.subagents.runner import Handle

    def handle(status: str, error: str = "", result: str = "") -> Handle:
        h = Handle(id="r", description="d", agent_type="reviewer", tier="main", depth=1, parent_sid="s")
        h.status, h.error, h.result = status, error, result
        return h

    ex = FanoutExecutor.__new__(FanoutExecutor)
    ex.progress = lambda *a, **k: None  # type: ignore[method-assign]
    ctx = SimpleNamespace(plan="", budget=AgentBudget(max_agents=9, max_tokens=1000))
    st = Subtask(id="t1", title="add files")
    worker_done = handle("completed", result="done")
    worker_done.diff = worker_done.diff_stat = ""

    async def spawn_returning(h: Handle):
        async def spawn(*args, **kwargs):
            return h

        ex._spawn = spawn  # type: ignore[method-assign]

    await spawn_returning(handle("failed", f"{CAP_ERROR}: 1700 > 1000"))
    with pytest.raises(BudgetStop) as stop:
        await ex._review(ctx, st, worker_done)
    assert stop.value.kind == "tokens"

    await spawn_returning(handle("failed", "provider exploded"))
    with pytest.raises(ReviewUnavailable, match="provider exploded"):  # asked twice, no verdict: not a pass
        await ex._review(ctx, st, worker_done)

    await spawn_returning(handle("completed", result="all good. VERDICT: pass"))
    assert await ex._review(ctx, st, worker_done) == ""
    await spawn_returning(handle("completed", result="broken. VERDICT: fail"))
    assert "VERDICT: fail" in await ex._review(ctx, st, worker_done)


async def test_a_reviewer_that_died_has_the_same_result_reviewed_again_not_reworked():
    """A dead reviewer rejected nothing. The worker used to be sent back anyway ("A reviewer rejected your result:
    the reviewer did not finish ..."), which cost a second worker and a second reviewer for work nobody had judged."""
    import asyncio
    from types import SimpleNamespace

    from k3code.autonomy.fanout import FanoutExecutor, Subtask
    from k3code.subagents.runner import Handle

    def handle(agent_type: str, status: str = "completed", error: str = "", result: str = "") -> Handle:
        h = Handle(id="h", description="d", agent_type=agent_type, tier="main", depth=1, parent_sid="s")
        h.status, h.error, h.result = status, error, result
        h.diff = h.diff_stat = ""
        h.worktree = SimpleNamespace(branch="k3/t1", path="/wt/t1", base="abc")  # type: ignore[assignment]
        h.merge = "merged"
        return h

    def dead() -> Handle:
        return handle("reviewer", "failed", "provider exploded")

    def verdict_of(v: str) -> Handle:
        return handle("reviewer", result=f"looked at it. VERDICT: {v}")

    async def play(*reviews: Handle) -> tuple[list[str], list[str], list[str], Subtask]:
        """Run one subtask whose reviewers answer in turn: (the states it went through, the reworks, the merges)."""
        states: list[str] = []
        reworks: list[str] = []
        merges: list[str] = []
        queue = list(reviews)
        done = handle("worker", result="done")

        async def spawn(ctx, st, agent_type, *args, **kwargs):
            return done if agent_type == "worker" else queue.pop(0)

        async def rework(ctx, st, problem):
            reworks.append(problem)
            return done

        async def merge(ctx, st):
            merges.append(st.id)

        ex = FanoutExecutor.__new__(FanoutExecutor)
        ex._spawn, ex._rework, ex._merge = spawn, rework, merge  # type: ignore[method-assign]
        ex.progress = lambda ctx, st, state, detail="": states.append(state)  # type: ignore[method-assign]
        ex.worker_prompt = lambda ctx, st: "work"  # type: ignore[method-assign]
        ctx = SimpleNamespace(
            sem=asyncio.Semaphore(1), budget=None, plan="", result=SimpleNamespace(stopped="", stopped_kind="")
        )
        st = Subtask(id="t1", title="add files")
        await ex._one(ctx, st)
        return states, reworks, merges, st

    # the reviewer dies, the next one passes it: merged, and the worker was never sent back
    states, reworks, merges, st = await play(dead(), verdict_of("pass"))
    assert reworks == [] and merges == ["t1"] and st.attempts == 0
    assert states == ["running", "reviewing"]

    # both die: the result stays unreviewed and unmerged, with no rework either
    states, reworks, merges, st = await play(dead(), dead())
    assert reworks == [] and merges == [] and states[-1] == "escalated" and st.attempts == 0

    # the second reviewer really rejects it: that is a rejection, and the worker is sent back once, as for any other
    states, reworks, merges, st = await play(dead(), verdict_of("fail"), verdict_of("pass"))
    assert len(reworks) == 1 and "VERDICT: fail" in reworks[0] and "did not finish" not in reworks[0]
    assert merges == ["t1"] and st.attempts == 1

    # and a plain rejection is reworked as before
    _, reworks, merges, _ = await play(verdict_of("fail"), verdict_of("pass"))
    assert len(reworks) == 1 and merges == ["t1"]
