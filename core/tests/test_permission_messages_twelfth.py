"""What the model and the person are told when a call is refused: a headless refusal with its own reason also says
nobody could approve it (and `k3code -p` counts it), and plan mode names what still works."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from k3code.cli import _unanswered_note
from k3code.permissions import decide
from k3code.permissions.engine import HEADLESS_ASK_DENIED, PLAN_HINT, PLAN_MSG


def _decide(tool: str, args: dict, cwd: Path, *, mode: str = "default", headless: bool = True):
    return decide(mode=mode, tool=tool, args=args, cwd=cwd, headless=headless)


# ── headless: a specific reason stays first, the headless sentence follows ──


@pytest.mark.parametrize(
    ("tool", "args", "reason"),
    [
        ("write", {"path": "../x.txt", "content": "x"}, "Outside project roots: "),
        ("read", {"path": "/etc/hostname"}, "Outside project roots: "),
        ("write", {"path": ".env", "content": "A=1"}, "Edits a file that runs as code or holds credentials; confirm"),
        ("edit", {"path": ".git/hooks/pre-commit", "old_string": "a", "new_string": "b"}, "Edits a file that runs as"),
    ],
)
def test_a_headless_refusal_with_its_own_reason_also_says_nobody_could_approve(tmp_path, tool, args, reason):
    (tmp_path / "proj").mkdir()
    d = _decide(tool, args, tmp_path / "proj")
    assert d.action == "deny", d
    assert d.message is not None and d.message.startswith(reason), d.message
    assert d.message.endswith(f". {HEADLESS_ASK_DENIED}"), d.message


def test_a_bare_headless_ask_keeps_the_plain_sentence(tmp_path):
    d = _decide("write", {"path": "new.py", "content": "y=1"}, tmp_path)
    assert d.action == "deny" and d.message == HEADLESS_ASK_DENIED
    d = _decide("bash", {"command": "echo hi > z.txt"}, tmp_path)
    assert d.action == "deny" and d.message == HEADLESS_ASK_DENIED


def test_a_force_push_asked_headless_keeps_its_reason_then_the_sentence(tmp_path):
    d = _decide("bash", {"command": "git push --force"}, tmp_path, mode="yolo")
    assert d.action == "deny" and d.message is not None
    assert "confirm it yourself" in d.message and d.message.endswith(f". {HEADLESS_ASK_DENIED}")


def test_an_interactive_ask_keeps_its_prompt_hint_without_the_headless_sentence(tmp_path):
    (tmp_path / "proj").mkdir()
    d = _decide("write", {"path": "../x.txt", "content": "x"}, tmp_path / "proj", headless=False)
    assert d.action == "ask" and d.message is not None and d.message.startswith("Outside project roots: ")
    assert HEADLESS_ASK_DENIED not in d.message


def test_a_deny_that_was_never_an_ask_gets_no_headless_sentence(tmp_path):
    d = _decide("write", {"path": "../x.txt", "content": "x"}, tmp_path, mode="auto")
    assert d.action == "deny" and HEADLESS_ASK_DENIED not in (d.message or "")
    d = _decide("bash", {"command": "rm -rf /"}, tmp_path)
    assert d.hardline and HEADLESS_ASK_DENIED not in (d.message or "")


# ── the note `k3code -p` prints on stderr ──


def _tool(name: str, result: str) -> dict:
    return {"id": name, "name": name, "arguments": {}, "result": result}


PLAIN = f"Error: {HEADLESS_ASK_DENIED}"
OUTSIDE = f"Error: Outside project roots: /etc/hostname. {HEADLESS_ASK_DENIED}"
CREDENTIAL = (
    f"Error: Edits a file that runs as code or holds credentials; confirm it yourself: /p/.env. {HEADLESS_ASK_DENIED}"
)


def test_the_note_counts_and_names_every_refusal_and_says_which_auto_would_allow():
    note = _unanswered_note(
        {"tools": [_tool("write", OUTSIDE), _tool("write", CREDENTIAL), _tool("read", OUTSIDE), _tool("bash", PLAIN)]}
    )
    assert note.startswith("k3code: 4 tool call(s) (bash, read, write) were refused")
    assert "1 (bash) would pass with `--permission auto`" in note and "headless_permission" in note
    assert "3 (read, write) need a path inside the project, or a person to confirm" in note


def test_the_note_for_plain_refusals_is_unchanged():
    note = _unanswered_note({"tools": [_tool("write", PLAIN), _tool("bash", PLAIN)]})
    assert note.startswith("k3code: 2 tool call(s) (bash, write) were refused: a -p run cannot ask for approval.")
    assert "`--permission auto` allows them" in note and "headless_permission" in note


def test_the_note_for_refusals_only_a_person_can_answer_does_not_offer_auto():
    note = _unanswered_note({"tools": [_tool("write", CREDENTIAL), _tool("read", OUTSIDE)]})
    assert note.startswith("k3code: 2 tool call(s) (read, write) were refused")
    assert "would not change that" in note and "a path inside the project" in note
    assert "allows them" not in note and "would pass" not in note and "headless_permission" not in note


def test_the_note_is_empty_when_nothing_was_refused_for_want_of_a_person():
    ran = {"tools": [_tool("glob", "a.py"), _tool("write", "Error: Hardline deny (env-dump): env"), _tool("x", "")]}
    assert _unanswered_note(ran) == ""
    assert _unanswered_note({}) == ""


def _run_p(tmp_path: Path, steps: list[dict]) -> subprocess.CompletedProcess[str]:
    """`k3code -p hi --json` on a scripted fake provider, headless_permission: ask (as test_cli_headless_output)."""
    home = tmp_path / "k3home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n- name: fake\n  kind: openai\n  base_url: http://fake\n  api_key_env: K3_TEST_KEY\n"
        "  models: {default: m}\npermission_mode: yolo\nmax_turns: 40\nheadless_permission: ask\n"
    )
    script = tmp_path / "script.json"
    script.write_text(json.dumps(steps))
    project = tmp_path / "proj"
    project.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("K3CODE_")}
    env |= {"K3CODE_HOME": str(home), "K3CODE_FAKE_PROVIDER": str(script), "K3_TEST_KEY": "x", "HOME": str(tmp_path)}
    code = "from k3code.cli import cli; cli()"
    argv = [sys.executable, "-c", code, "-p", "hi", "--json"]
    return subprocess.run(argv, cwd=project, env=env, capture_output=True, text=True, timeout=120)


def test_a_headless_run_counts_the_refusals_with_a_reason_and_tells_the_model(tmp_path):
    calls = [
        ("write", {"path": "../x.txt", "content": "x"}),
        ("write", {"path": ".env", "content": "A=1"}),
        ("read", {"path": "/etc/hostname"}),
        ("write", {"path": "new.py", "content": "y=1"}),
    ]
    steps = [
        {"type": "tool_call", "id": f"c{i}", "name": n, "arguments": a, "tool_results": i}
        for i, (n, a) in enumerate(calls)
    ]
    steps.append({"type": "text", "text": "all done", "tool_results": len(calls)})
    res = _run_p(tmp_path, steps)
    results = [t["result"] for t in json.loads(res.stdout)["tools"]]
    assert len(results) == 4 and all(HEADLESS_ASK_DENIED in r for r in results), results
    assert results[0].startswith("Error: Outside project roots: ") and results[3] == f"Error: {HEADLESS_ASK_DENIED}"
    assert "4 tool call(s) (read, write) were refused" in res.stderr
    assert "1 (write) would pass with `--permission auto`" in res.stderr
    assert "3 (read, write) need a path inside the project" in res.stderr


# ── plan mode ──


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("bash", {"command": "ls"}),
        ("bash", {"command": "git log --oneline"}),
        ("write", {"path": "x.txt", "content": "x"}),
        ("edit", {"path": "a.txt", "old_string": "a", "new_string": "b"}),
    ],
)
def test_plan_mode_names_what_still_works(tmp_path, tool, args):
    (tmp_path / "a.txt").write_text("a\n")
    d = _decide(tool, args, tmp_path, mode="plan", headless=False)
    assert d.action == "deny" and d.message == PLAN_MSG
    assert d.message == f"Plan mode is read-only; bash/write/edit are unavailable: {PLAN_HINT}"
    assert PLAN_HINT == "explore with read, glob and grep, then call exit_plan."


@pytest.mark.parametrize("tool", ["web_fetch", "web_search", "mcp__hub__run"])
def test_plan_mode_names_the_other_tool_it_refuses(tmp_path, tool):
    d = _decide(tool, {}, tmp_path, mode="plan", headless=False)
    assert d.action == "deny"
    assert d.message == f"Plan mode is read-only; {tool} is unavailable: {PLAN_HINT}"


def test_plan_mode_still_allows_what_it_names(tmp_path):
    for tool, args in (("read", {"path": "a.txt"}), ("glob", {"pattern": "*"}), ("grep", {"pattern": "a"})):
        assert _decide(tool, args, tmp_path, mode="plan", headless=False).action == "allow", tool
    assert _decide("exit_plan", {}, tmp_path, mode="plan", headless=False).action == "allow"


@pytest.mark.parametrize("tool", ["bash", "write", "edit", "web_fetch", "mcp__hub__run"])
def test_plan_mode_refusals_stay_policy_denials_for_the_error_memory(tmp_path, tool):
    # toolerrors.py tells a refusal by policy from an error of the project by how the message starts
    from k3code.toolerrors import failure_of

    d = _decide(
        tool,
        {"command": "ls", "path": "x", "content": "x", "old_string": "a", "new_string": "b"},
        tmp_path,
        mode="plan",
        headless=False,
    )
    failure = failure_of(tool, {}, {"error": d.message})
    assert failure is not None and failure.error_class == "denied"
