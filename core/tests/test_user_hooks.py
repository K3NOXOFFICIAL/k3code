"""User hooks with Claude Code's contract: JSON on stdin, exit 2 blocks, JSON decisions, timeouts, trust."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from k3code import paths, trust, userhooks
from k3code.agent.loop import AgentLoop
from k3code.providers.types import ToolCall
from k3code.router import Router, build_chain
from k3code.userhooks import Hook, HookRunner
from test_autonomy_gateway import events, k3home, make, run_turn, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}


class _NoProvider:
    name = "none"
    base_url = "https://none.test"

    async def stream(self, *a, **k):  # pragma: no cover - the tests call tools directly
        raise AssertionError("no model call expected")
        yield

    async def aclose(self) -> None:
        pass


def _loop(cwd: Path, hooks: list[Hook], mode: str = "yolo", approval=None) -> AgentLoop:
    router = Router(build_chain([_NoProvider()], [["m"]]), max_retries=0)
    loop = AgentLoop(
        router, system_prompt="t", permission_mode=mode, cwd=cwd, approval_callback=approval, headless=False
    )
    loop.hooks = HookRunner(hooks, session_id="s-1", cwd=cwd)
    return loop


def _bash(command: str) -> ToolCall:
    return ToolCall(id="c1", name="bash", arguments={"command": command})


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


async def test_exit_2_blocks_and_stderr_reaches_the_model(proj: Path) -> None:
    loop = _loop(proj, [Hook("PreToolUse", "echo 'no touching today' >&2; exit 2", matcher="bash")])
    result = await loop._execute_tool(_bash("touch ran"))
    assert result == {"error": "Blocked by a PreToolUse hook: no touching today"}
    assert not (proj / "ran").exists()


async def test_stdin_carries_the_event_as_json(proj: Path) -> None:
    pre, post = proj.parent / "pre.json", proj.parent / "post.json"
    loop = _loop(proj, [Hook("PreToolUse", f"cat > {pre}"), Hook("PostToolUse", f"cat > {post}")])
    await loop._execute_tool(_bash("echo hello"))
    data = json.loads(pre.read_text())
    assert data == {
        "session_id": "s-1",
        "cwd": str(proj),
        "hook_event_name": "PreToolUse",
        "tool_name": "bash",
        "tool_input": {"command": "echo hello"},
    }
    after = json.loads(post.read_text())
    assert after["hook_event_name"] == "PostToolUse" and after["tool_response"] == "hello"


async def test_json_decision_block_and_post_tool_feedback(proj: Path) -> None:
    block = Hook("PreToolUse", """echo '{"decision": "block", "reason": "policy says no"}'""")
    assert (await _loop(proj, [block])._execute_tool(_bash("true")))["error"].endswith("policy says no")
    note = Hook("PostToolUse", """echo '{"additionalContext": "lint is red"}'""")
    result = await _loop(proj, [note])._execute_tool(_bash("echo out"))
    assert result["content"] == "out\n\n[PostToolUse hook] lint is red"


async def test_approve_answers_the_prompt_but_never_a_hardline_deny(proj: Path) -> None:
    async def no_prompt(*a, **k):
        raise AssertionError("the hook approved: no approval prompt expected")

    approve = Hook("PreToolUse", """echo '{"decision": "approve"}'""")
    loop = _loop(proj, [approve], mode="ask", approval=no_prompt)
    result = await loop._execute_tool(ToolCall(id="w", name="write", arguments={"path": "a.txt", "content": "x"}))
    assert "error" not in result and (proj / "a.txt").read_text() == "x"
    secret = Path.home() / ".ssh" / "id_ed25519"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("KEY")
    read = await loop._execute_tool(ToolCall(id="r", name="read", arguments={"path": str(secret)}))
    assert "Hardline deny" in read["error"] and "KEY" not in json.dumps(read)


async def test_timeout_and_other_failures_block_nothing(proj: Path, caplog) -> None:
    slow = Hook("PreToolUse", "sleep 30", timeout=0.3)
    failing = Hook("PreToolUse", "echo broken >&2; exit 1")
    begin = time.monotonic()
    result = await _loop(proj, [slow, failing])._execute_tool(_bash("echo ran"))
    assert time.monotonic() - begin < 10
    assert result.get("stdout", "").strip() == "ran"
    assert "timed out" in caplog.text and "exited 1" in caplog.text


async def test_matcher_is_a_case_insensitive_tool_regex(proj: Path) -> None:
    hooks = [Hook("PreToolUse", "exit 2", matcher="edit|write"), Hook("PreToolUse", "exit 0", matcher="Bash")]
    runner = HookRunner(hooks)
    assert [h.command for h in runner.for_event("PreToolUse", "bash")] == ["exit 0"]
    assert [h.command for h in runner.for_event("PreToolUse", "write")] == ["exit 2"]


async def test_hook_env_is_scrubbed(proj: Path, monkeypatch) -> None:
    monkeypatch.setenv("K3CODE_API_KEY", "sk-secret-value")
    monkeypatch.setenv("K3CODE_GATEWAY_TOKEN", "gw-token-value")
    dump = proj.parent / "env.txt"
    await _loop(proj, [Hook("PreToolUse", f"env > {dump}")])._execute_tool(_bash("true"))
    env = dump.read_text()
    assert "sk-secret-value" not in env and "gw-token-value" not in env
    assert f"CLAUDE_PROJECT_DIR={proj}" in env


def test_user_hooks_always_project_hooks_only_when_trusted(proj: Path) -> None:
    paths.home().mkdir(parents=True, exist_ok=True)
    (paths.home() / "config.yaml").write_text("hooks:\n  Stop:\n    - {command: user-stop}\n", encoding="utf-8")
    cfg = proj / ".k3code" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        "hooks:\n  PreToolUse:\n    - matcher: bash\n"
        "      hooks: [{type: command, command: project-check, timeout: 5}]\n",  # Claude Code's nested form
        encoding="utf-8",
    )
    assert [h.command for h in userhooks.load(proj).hooks] == ["user-stop"]
    assert any("hook PreToolUse on bash runs as you, unsandboxed: project-check" in s for s in trust.summary(proj))
    trust.record(proj, trusted=True)
    loaded = userhooks.load(proj).hooks
    assert [(h.source, h.command, h.timeout) for h in loaded] == [
        ("user", "user-stop", 60.0),
        ("project", "project-check", 5.0),
    ]


async def test_gateway_user_prompt_submit_blocks_or_adds_context(tmp_path: Path, monkeypatch) -> None:
    steps = [
        {"type": "text", "match": "CTX-FROM-HOOK", "text": "saw the hook context"},
        {"type": "text", "text": "no context"},
    ]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    home = k3home(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    started = tmp_path.parent / "started.json"
    (home / "config.yaml").write_text(
        "hooks:\n"
        f"  SessionStart: [{{command: 'cat > {started}'}}]\n"
        "  UserPromptSubmit:\n"
        "    - {command: \"grep -q forbidden && { echo 'not that' >&2; exit 2; }; echo CTX-FROM-HOOK\"}\n",
        encoding="utf-8",
    )
    await start(server, tmp_path)
    await run_turn(server, "hello there")
    assert json.loads(started.read_text())["source"] == "startup"
    assert any("saw the hook context" in e.get("text", "") for e in events(server, "message.delta"))
    await run_turn(server, "do the forbidden thing")
    deltas = [e.get("text", "") for e in events(server, "message.delta")]
    assert "Prompt blocked by a UserPromptSubmit hook: not that" in deltas
