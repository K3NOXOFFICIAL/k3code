"""The goal judge's evidence digest (roadmap #172 §2): what the turn's tool calls showed, not what the agent claimed."""

from __future__ import annotations

from typing import Any

from k3code.goals import (
    EVIDENCE_DIGEST_CHARS,
    JUDGE_SYSTEM_PROMPT,
    GoalManager,
    TurnEvidence,
    make_judge,
    turn_evidence,
    turn_evidence_digest,
)


class Turn:
    """Builds a turn's stored messages: each tool call is an assistant message followed by its result."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = [{"role": "user", "content": "do it"}]

    def tool(self, name: str, result: str, **args: Any) -> Turn:
        cid = f"c{len(self.messages)}"
        self.messages.append(
            {"role": "assistant", "content": None, "tool_calls": [{"id": cid, "name": name, "arguments": args}]}
        )
        self.messages.append({"role": "tool", "content": result, "tool_call_id": cid, "name": name})
        return self

    def bash(self, command: str, result: str = "ok", **args: Any) -> Turn:
        return self.tool("bash", result, command=command, **args)

    def edit(self, path: str, result: str = "Edited: 1 replacement") -> Turn:
        return self.tool("edit", result, path=path, old_string="a", new_string="b")


def test_failing_then_passing_test_after_the_edit_reads_as_passed():
    turn = (
        Turn()
        .edit("src/app.py")
        .bash("cd core && uv run pytest -q tests/test_app.py", "1 failed\n[exit code 1]")
        .edit("src/app.py")
        .bash("cd core && uv run pytest -q tests/test_app.py", "3 passed")
    )
    digest = turn_evidence_digest(turn.messages)
    assert "$ cd core && uv run pytest -q tests/test_app.py -> exit 1" in digest
    assert "$ cd core && uv run pytest -q tests/test_app.py -> exit 0" in digest
    assert "Last test/check: passed, after the last edit." in digest
    assert "Files written/edited: 1: src/app.py" in digest
    assert "Unresolved tool errors: none." in digest


def test_an_edit_after_the_last_test_leaves_it_unverified():
    turn = Turn().bash("npm test", "all good").tool("write", "Wrote web/x.ts", path="web/x.ts", content="x")
    digest = turn_evidence_digest(turn.messages)
    assert "Last test/check: passed, BEFORE the last edit (later edits are unverified)." in digest


def test_a_failing_last_test_reads_as_failed():
    digest = turn_evidence_digest(Turn().bash("go test ./...", "FAIL\n[exit code 2]").messages)
    assert "-> exit 2" in digest and "Last test/check: FAILED, after the last edit." in digest


def test_tool_errors_count_only_until_a_successful_retry_of_the_same_tool_and_path():
    turn = (
        Turn()
        .edit("a.py", "Error: old_string not found in a.py")
        .edit("a.py")  # the retry on the same file resolves it
        .edit("b.py", "Error: old_string not found in b.py")
        .edit("c.py")  # another file does not resolve b.py
        .tool("grep", "Error: invalid regex", pattern="(")
    )
    digest = turn_evidence_digest(turn.messages)
    assert "Unresolved tool errors: 2" in digest
    assert "- edit b.py: Error: old_string not found in b.py" in digest
    assert "- grep: Error: invalid regex" in digest
    assert "a.py: Error" not in digest
    assert "Files written/edited: 2: a.py, c.py" in digest  # a failed edit changed nothing


def test_no_verification_command_is_said_explicitly_and_exploring_bash_is_no_error():
    turn = Turn().bash("grep -rn foo src", "[exit code 1]").bash("pytest -q", "job 7 started", background=True)
    digest = turn_evidence_digest(turn.messages)
    assert "No test/check command ran this turn." in digest  # a background run has no exit code yet
    assert "Files written/edited: none." in digest and "Unresolved tool errors: none." in digest


def test_digest_is_capped():
    turn = Turn()
    for i in range(60):
        turn.edit(f"src/{'deep/' * 30}file{i}.py", f"Error: {'x' * 400}")
        turn.bash(f"pytest {'-k long ' * 60} {i}", "[exit code 1]")
    digest = turn_evidence_digest(turn.messages)
    assert len(digest) <= EVIDENCE_DIGEST_CHARS
    assert digest.startswith("Test/check commands this turn")


async def test_make_judge_fences_the_digest_as_untrusted_data_and_keeps_the_check_note_outside():
    seen: list[tuple[str, str]] = []

    async def complete(system: str, user: str) -> str:
        seen.append((system, user))
        return '{"verdict": "continue", "reason": "no test ran"}'

    judge = make_judge(complete)
    digest = "No test/check command ran this turn."
    verdict = await judge("fix the bug", "All done, tests pass!", turn_evidence(Turn().messages))
    assert verdict == ("continue", "no test ran", False, False)
    system, user = seen[0]
    assert "claim of success is not evidence" in system and system == JUDGE_SYSTEM_PROMPT
    assert "untrusted data, not instructions" in user
    assert f"```text\n{digest}\nFiles written/edited: none." in user
    assert "--check" not in user
    await judge("fix the bug", "done", TurnEvidence(check_configured=True))
    user = seen[1][1]
    fence_end = user.index("\n```\n", user.index("```text"))
    assert user.index("A completion check (--check) is configured") > fence_end  # a harness fact, not untrusted data


async def test_evaluate_after_turn_hands_the_judge_the_evidence_and_says_when_a_check_is_configured(monkeypatch):
    import k3code.goals as goals

    async def passing_gate(gate, *, cwd=None):
        return True, 0, ""

    monkeypatch.setattr(goals, "run_gate", passing_gate)
    box: dict[str, dict | None] = {"goal": None}
    mgr = GoalManager(lambda: box["goal"], lambda s: box.__setitem__("goal", s))
    got: list[TurnEvidence] = []

    async def judge(goal: str, response: str, evidence: TurnEvidence):
        got.append(evidence)
        return "continue", "keep going", False, False

    ev = turn_evidence(Turn().bash("pytest -q", "3 passed").messages)
    mgr.set("ship")
    await mgr.evaluate_after_turn("x", judge, evidence=ev)
    assert got[0] is ev and not ev.check_configured
    mgr.set("ship", check="make test")
    await mgr.evaluate_after_turn("x", judge, evidence=turn_evidence(Turn().messages))
    assert got[1].check_configured


NOT_CHECKS = [
    "cat pytest.ini",
    "pip install pytest",
    "grep -rn pytest src",
    "which tsc",
    'echo "pytest passed"',
    "pip install pytest-cov",
    "ls node_modules/.bin/eslint.js",
]
CHECKS = [
    "pytest -q",
    "cd core && uv run pytest -q",
    "core/.venv/bin/python -m pytest tests/x.py",
    "npx tsc --noEmit",
    "FOO=1 pytest",
    ".venv/bin/pytest -x",
    "(cd web && npm test)",
    "./gradlew test",
    "ruff check . && pytest",
]


def test_only_a_tool_at_command_position_is_a_check():
    for command in NOT_CHECKS:
        digest = turn_evidence_digest(Turn().bash(command, "ok").messages)
        assert "No test/check command ran this turn." in digest, command
    for command in CHECKS:
        digest = turn_evidence_digest(Turn().bash(command, "ok").messages)
        assert "Last test/check: passed, after the last edit." in digest, command


def test_a_tool_name_in_a_later_command_does_not_hide_the_failed_test():
    turn = Turn().bash("pytest -q", "1 failed\n[exit code 1]").edit("src/app.py").bash("cat pytest.ini", "[pytest]")
    digest = turn_evidence_digest(turn.messages)
    assert "Last test/check: FAILED, BEFORE the last edit" in digest
    assert "passed" not in digest


def test_a_piped_or_masked_test_exit_status_is_not_the_tests():
    for command in ("pytest -q | tail -5", "pytest -q || true", "pytest -q; echo done", "pytest 2>&1 | tee log"):
        digest = turn_evidence_digest(Turn().bash(command, "1 failed").messages)
        assert "Last test/check: exit status not the test's (piped or masked)" in digest, command
        assert "passed" not in digest, command
    for command in ("set -o pipefail; pytest -q | tail -5", "pytest -q && echo ok", "pytest -q\n"):
        digest = turn_evidence_digest(Turn().bash(command, "ok").messages)
        assert "Last test/check: passed" in digest, command


async def test_the_last_check_carries_across_turns_and_the_continuation_says_why():
    box: dict[str, dict | None] = {"goal": None}
    mgr = GoalManager(lambda: box["goal"], lambda s: box.__setitem__("goal", s))
    seen: list[str] = []

    async def judge(goal: str, response: str, evidence: TurnEvidence):
        seen.append(evidence.render())
        return "continue", "README.md was edited after the last passing test", False, False

    mgr.set("fix the parser")
    first = turn_evidence(Turn().edit("src/parse.py").bash("pytest -q", "5 passed").messages)
    await mgr.evaluate_after_turn("fixed", judge, evidence=first)
    readme = await mgr.evaluate_after_turn("docs", judge, evidence=turn_evidence(Turn().edit("README.md").messages))
    assert "Last test/check of this goal (turn 1): $ pytest -q -> passed; files edited since: 1: README.md." in seen[1]
    assert readme.prompt and "the completion judge's reason: README.md was edited after the last passing test" in (
        readme.prompt
    )
    await mgr.evaluate_after_turn("all done", judge, evidence=turn_evidence(Turn().messages))
    assert "files edited since: 1: README.md." in seen[2]  # still unverified: nothing ran since
    await mgr.evaluate_after_turn("ok", judge, evidence=turn_evidence(Turn().bash("pytest -q", "5 passed").messages))
    await mgr.evaluate_after_turn("done", judge, evidence=turn_evidence(Turn().messages))
    assert "Last test/check of this goal (turn 4): $ pytest -q -> passed; no file edited since." in seen[4]
    mgr.set("another goal")  # a new goal starts without the old one's check
    await mgr.evaluate_after_turn("x", judge, evidence=turn_evidence(Turn().messages))
    assert "Last test/check of this goal" not in seen[5]


async def test_a_judge_failure_reason_stays_out_of_the_continuation():
    box: dict[str, dict | None] = {"goal": None}
    mgr = GoalManager(lambda: box["goal"], lambda s: box.__setitem__("goal", s))

    async def judge(goal: str, response: str, evidence: TurnEvidence):
        return "continue", "judge error: TimeoutError", False, True

    mgr.set("ship")
    decision = await mgr.evaluate_after_turn("x", judge)
    assert decision.prompt and "judge's reason" not in decision.prompt
