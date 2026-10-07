"""Fan-out executor: parallel worktree children, reviewer gate, merge + tests, retry once, escalation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from k3code.autonomy.fanout import detect_test_command, extract_subtasks, parse_verdict
from k3code.autonomy.scope import ScopeVerdict
from m1cmd_helpers import git_repo
from test_autonomy_gateway import PLAN, call, events, make, run_turn


def classify(subs: list[str]) -> dict:
    return {"type": "text", "match": "You classify a coding task",
            "text": json.dumps({"scope": "large", "needs_plan": True, "risk": "low", "parallelizable": True,
                                "suggested_subtasks": subs, "reason": "big"})}


PLANNER = [{"type": "tool_call", "match": "PLANNING mode", "when": "first", "name": "exit_plan",
            "arguments": {"plan": PLAN}}]


def worker(i: int, name: str, *, sleep: float = 0.0, content: str = "ok\n") -> list[dict]:
    key = f"YOUR subtask (t{i})"
    steps: list[dict] = []
    if sleep:
        steps.append({"type": "tool_call", "id": "s", "match": key, "when": "first", "name": "bash",
                      "arguments": {"command": f"sleep {sleep}"}})
    steps += [
        {"type": "tool_call", "id": "w", "match": key, "when": "first", "name": "write",
         "arguments": {"path": name, "content": content}},
        {"type": "text", "match": key, "when": "after_tool", "text": f"worker {i} done"},
    ]
    return steps


def reviewer(title: str, verdict: str = "pass", *, after: str = "") -> dict:
    s = {"type": "text", "match": f"Subtask: {title}", "text": f"review of {title}. VERDICT: {verdict}"}
    return s


def make_fan(tmp: Path, monkeypatch, steps: list[dict], repo: Path, **fan):
    cfg = {"plan_first": True, "proposals": False, "advisor_on_plan": False,
           "fanout": {"enabled": True, "max_parallel": 3, "require_tests": True, **fan}}
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


def steps_for(subs: list[str], extra: list[dict] | None = None, reviews: list[dict] | None = None,
              sleep: float = 0.15) -> list[dict]:
    steps = [classify(subs), *PLANNER]
    for i, _ in enumerate(subs, 1):
        steps += worker(i, f"f{i}.txt", sleep=sleep)
    steps += reviews or [reviewer(t) for t in subs]
    steps += extra or []
    return steps


async def test_fanout_runs_children_in_parallel_reviews_merges_and_tests_each_merge(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    log = tmp_path / "testlog.txt"
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
        {"type": "tool_call", "id": "w2", "match": "REWORK", "when": "first", "name": "write",
         "arguments": {"path": "f2.txt", "content": "better\n"}},
        {"type": "text", "match": "REWORK", "when": "after_tool", "text": "worker 2 fixed"},
    ]
    reviews = [
        reviewer("SUB-ONE add f1"),
        {"type": "text", "match": "Subtask: SUB-TWO add f2", "text": "no tests added. VERDICT: fail"},
    ]
    # after the rework the reviewer sees "worker 2 fixed" in the worker report and passes it
    steps = [classify(subs), *PLANNER, *worker(1, "f1.txt"), *worker(2, "f2.txt"), *rework, *reviews,
             {"type": "text", "match": "worker 2 fixed", "text": "now fine. VERDICT: pass"}]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="true")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert done["ok"], done["summary"]
    assert (repo / "f2.txt").read_text() == "better\n"
    states = [p["state"] for p in events(server, "fanout.progress") if p["subtask_id"] == "t2"]
    assert "retrying" in states and states[-1] == "merged"


async def test_failing_tests_are_sent_back_once_then_pass(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE add f1", "SUB-TWO add bad"]
    steps = [classify(subs), *PLANNER, *worker(1, "f1.txt"), *worker(2, "bad.txt"),
             reviewer(subs[0]), reviewer(subs[1]),
             {"type": "tool_call", "id": "rm", "match": "tests failed after merging", "when": "first",
              "name": "bash", "arguments": {"command": "rm bad.txt && echo fixed > f2.txt"}},
             {"type": "text", "match": "tests failed after merging", "when": "after_tool", "text": "worker 2 fixed"}]
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="test ! -f bad.txt")
    await start_in(server, repo)
    await run_turn(server, "do the big thing")
    done = events(server, "fanout.done")[0]
    assert done["ok"] and done["tests"] == "pass", done["summary"]
    assert not (repo / "bad.txt").exists() and (repo / "f2.txt").exists()
    t2 = [p for p in events(server, "fanout.progress") if p["subtask_id"] == "t2"]
    assert any(p["state"] == "retrying" and "tests failed" in p["detail"] for p in t2)
    assert git(repo, "log", "--oneline", "--merges").count("Merge k3/") == 2


async def test_persistent_failure_escalates_to_parent_and_keeps_branch(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    subs = ["SUB-ONE add f1", "SUB-TWO add bad"]
    steps = [classify(subs), *PLANNER, *worker(1, "f1.txt"), *worker(2, "bad.txt"),
             reviewer(subs[0]), reviewer(subs[1]),
             # the retry does not fix anything
             {"type": "text", "match": "--- REWORK ---", "text": "I could not fix it"},
             {"type": "text", "match": "needs you (escalated)", "text": "parent handled it"}]
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
    steps = [classify(subs), *PLANNER,
             *worker(1, "shared.txt", content="from one\n"), *worker(2, "shared.txt", content="from two\n"),
             reviewer(subs[0]), reviewer(subs[1]),
             {"type": "tool_call", "id": "fix", "match": "conflicts in", "when": "first", "name": "write",
              "arguments": {"path": "shared.txt", "content": "from one\nfrom two\n"}},
             {"type": "text", "match": "conflicts in", "when": "after_tool", "text": "worker resolved"}]
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

    h = Handle(id="sa-1", description="do x", agent_type="worker", tier="cheap", depth=1, parent_sid="p",
               status="running")
    fake = SimpleNamespace(live={}, subagents=SimpleNamespace(handles={"sa-1": h}))
    rows = GatewayServer._active_rows(fake, None)
    assert rows[0]["id"] == "sa-1" and rows[0]["origin"] == "subagent" and rows[0]["background"]
