"""A fan-out turn's goal judge sees what the children did: their tests with exit codes, the files merged and the
tests on the merged result. The parent's stored turn is only the task and the fan-out's summary, so the judge's
digest said "No test/check command ran this turn" and "Files written/edited: none" for work that was done and
tested."""

from __future__ import annotations

import subprocess

from k3code.autonomy import fanout as fan_mod
from k3code.goals import TurnEvidence
from k3code.reliability import sandbox
from m1cmd_helpers import git_repo
from test_autonomy_gateway import events, run_turn
from test_fanout import PLANNER, SUBS, classify, make_fan, reviewer, start_in


def worker_with_test(i: int, name: str) -> list[dict]:
    """A worker that writes its file with bash and runs the project's tests in its worktree, then reports."""
    key = f"YOUR subtask (t{i})"
    command = f"echo ok > {name} && tools/pytest -q"
    return [
        {
            "type": "tool_call",
            "id": "b",
            "match": key,
            "when": "first",
            "name": "bash",
            "arguments": {"command": command},
        },
        {"type": "text", "match": key, "when": "after_tool", "text": f"worker {i} done"},
    ]


def _fan_server(tmp_path, monkeypatch, steps: list[dict], **autonomy):
    """A fan-out server whose sandbox-only parts are stubbed: no bwrap probe, and the merge tests pass."""

    async def no_preflight(*args, **kwargs):
        return ""

    runs: list[str] = []

    async def merged_tests(cmd, cwd, timeout=600):
        runs.append(cmd)
        return True, "3 passed"

    monkeypatch.setattr(fan_mod, "preflight", no_preflight)
    monkeypatch.setattr(fan_mod, "run_tests", merged_tests)
    monkeypatch.setattr(sandbox, "should_sandbox", lambda *a, **k: False)  # bwrap may be missing here
    repo = git_repo(tmp_path / "repo")
    stub = repo / "tools" / "pytest"  # the workers' test runner: a stub that passes
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\necho '3 passed'\n")
    stub.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "test stub"], cwd=repo, check=True, capture_output=True)
    server = make_fan(tmp_path, monkeypatch, steps, repo, test_command="pytest -q")
    server.config.autonomy.update({"advisor_on_goal": False, **autonomy})
    seen: list[TurnEvidence] = []

    async def judge(goal, response, evidence):
        seen.append(evidence)
        return "done", "merged and tested", False, False

    server.goal_judge = judge
    return server, repo, seen, runs


async def test_a_goal_turn_that_fans_out_is_judged_on_the_childrens_tests_and_edits(tmp_path, monkeypatch):
    subs = SUBS[:2]
    steps = [classify(subs), *PLANNER, *worker_with_test(1, "f1.txt"), *worker_with_test(2, "f2.txt")]
    steps += [reviewer(t) for t in subs]
    server, repo, seen, runs = _fan_server(tmp_path, monkeypatch, steps)
    await start_in(server, repo)
    server.goal_manager(server.session).set("build the whole feature")
    await run_turn(server, "go")
    assert events(server, "fanout.done")[0]["ok"] and runs == ["pytest -q", "pytest -q"]
    assert len(seen) == 1
    digest = seen[0].render()
    assert "No test/check command ran this turn." not in digest
    checks = [(command, code) for _, command, code, _ in seen[0].checks]
    for i in (1, 2):  # each worker's own test in its worktree, then the tests on the merged result
        worker = (f"echo ok > f{i}.txt && tools/pytest -q", 0)
        merged = (f"pytest -q  # fan-out: tests on the merged result of t{i}", 0)
        assert worker in checks and merged in checks and checks.index(worker) < checks.index(merged)
    assert "$ pytest -q # fan-out: tests on the merged result of t" in digest
    assert "Last test/check: passed, after the last edit." in digest
    assert "Files written/edited: 2:" in digest and "f1.txt" in digest and "f2.txt" in digest
    assert str(repo) not in digest  # the workers' worktree paths read as the project's


async def test_the_parent_finishing_a_partial_fan_out_is_judged_with_the_merged_childs_work(tmp_path, monkeypatch):
    """One child merges, the other changes nothing: the parent finishes the task in the same turn, and the implicit
    goal's judge sees the merged child's work and tests before the parent's own calls."""
    subs = SUBS[:2]
    steps = [classify(subs), *PLANNER, *worker_with_test(1, "f1.txt")]
    steps += [{"type": "text", "match": "YOUR subtask (t2)", "text": "nothing to change"}, reviewer(subs[0])]
    steps += [{"type": "text", "match": "Finish the remaining work yourself", "text": "finished the rest"}]
    server, repo, seen, runs = _fan_server(tmp_path, monkeypatch, steps, auto_continue=True)
    await start_in(server, repo)
    await run_turn(server, "build the whole feature")
    done = events(server, "fanout.done")[0]
    assert not done["ok"] and done["merged"] == 1 and runs == ["pytest -q"]
    assert len(seen) == 1
    digest = seen[0].render()
    assert "$ pytest -q # fan-out: tests on the merged result of t1 -> exit 0" in digest
    assert "$ echo ok > f1.txt && tools/pytest -q -> exit 0" in digest
    assert "Files written/edited: 1: f1.txt" in digest
    assert "Last test/check: passed, after the last edit." in digest
