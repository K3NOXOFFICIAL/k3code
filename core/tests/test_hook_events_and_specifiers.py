"""Claude Code compatibility: hooks answer with ``permissionDecision`` and fire on more events, and ``permissions``
reads ``allow`` / ``ask`` / ``deny`` lists of ``Tool(argument)`` rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from k3code import confio, userhooks
from k3code.permissions import decide
from k3code.permissions.rules import Rule, evaluate, expand, from_config, parse_specifier
from k3code.providers.types import ToolCall
from k3code.userhooks import EVENTS, Hook, HookRunner
from m1cmd_helpers import cmd, make_server, new_session
from test_ops_tools import _Answer, _stub_bash_loop
from test_session_ai import _long_transcript

# --- Tool(argument) rules


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("Bash(git push:*)", ("bash", "git push *")),
        ("Bash(npm run test)", ("bash", "npm run test")),
        ("Bash", ("bash", "*")),
        ("Bash(*)", ("bash", "*")),
        ("Read(~/.ssh/**)", ("read", "~/.ssh/**")),
        ("Write(src/**)", ("edit", "src/**")),
        ("Edit(src/**)", ("edit", "src/**")),
        ("mcp__github__create_issue", ("mcp__github__create_issue", "*")),
        ("  bash( ls :* )  ", ("bash", "ls *")),
        ("Grep(/src/**)", ("read", "/src/**")),  # the engine decides every read-only file tool as `read`
        ("WebFetch(domain:example.com)", ("web_fetch", "example.com")),
        ("WebSearch", ("web_search", "*")),
        ("mcp__github", ("mcp__github__*", "*")),  # the whole server
        ("Task", ("task", "*")),  # a tool that does not exist here keeps its own name: harmless, not a deny of all
        ("Frobnicate(x)", ("frobnicate", "x")),
        ("", None),
        ("Bash(", None),
        ("(git push)", None),
    ],
)
def test_parse_specifier(spec: str, expected: tuple[str, str] | None) -> None:
    assert parse_specifier(spec) == expected


def test_the_lists_become_rules_in_order() -> None:
    rules = from_config({"allow": ["Bash(git status:*)", "Read"], "ask": ["Bash(git push:*)"], "deny": ["Edit(.env)"]})
    assert [(r.tool, r.pattern, r.action) for r in rules] == [
        ("bash", "git status *", "allow"),
        ("read", "*", "allow"),
        ("bash", "git push *", "ask"),
        ("edit", "*/.env", "deny"),  # relative: matched in any directory, the engine matches absolute paths
        ("edit", "*/.env/*", "deny"),  # and what is inside it, if the name is a directory
    ]


def test_the_lists_mix_with_the_native_form() -> None:
    rules = from_config({"allow": ["Bash(ls:*)"], "bash": {"rm *": "deny"}})
    assert Rule("bash", "ls *", "allow") in rules and Rule("bash", "rm *", "deny") in rules


def test_the_longer_pattern_wins_as_ever() -> None:
    rules = from_config({"allow": ["Bash(git push:*)"], "deny": ["Bash(git push --force:*)"]})
    assert evaluate("bash", "git push origin main", rules).action == "allow"
    assert evaluate("bash", "git push --force origin main", rules).action == "deny"


def test_an_allow_that_cannot_be_read_is_dropped_and_reported() -> None:
    problems: list[str] = []
    rules = from_config({"allow": ["Bash(ls:*)", "(git push)"]}, problems)
    assert [(r.pattern, r.action) for r in rules] == [("ls *", "allow")]  # a dropped allow opens nothing
    assert len(problems) == 1 and "allow entry 2" in problems[0] and "git push" not in problems[0]


@pytest.mark.parametrize("action", ["ask", "deny"])
def test_an_ask_or_deny_that_cannot_be_read_stops_everything(action: str) -> None:
    problems: list[str] = []
    rules = from_config({action: ["(git push)"]}, problems)
    assert rules == [Rule("*", "*", "deny")] and problems  # what it meant to stop is unknown: fail closed
    assert evaluate("bash", "ls", rules).action == "deny"


def test_without_a_problem_list_an_unreadable_entry_raises() -> None:
    with pytest.raises(ValueError, match="allow entry 1"):
        from_config({"allow": ["(git push)"]})


def test_config_validation_accepts_the_lists_and_rejects_a_bad_one() -> None:
    confio.validate({"permissions": {"allow": ["Bash(ls:*)"], "deny": ["Read(.env)"]}})
    with pytest.raises(ValueError):
        confio.validate({"permissions": {"deny": ["(git push)"]}})


def test_a_tool_that_does_not_exist_here_does_not_deny_everything() -> None:
    """A Claude Code settings file denies `Task` or `TodoWrite`; that must not brick every read and command."""
    rules = from_config({"deny": ["Task", "TodoWrite", "Agent(reviewer)"]})
    assert evaluate("bash", "ls", rules).action != "deny" and evaluate("read", "/p/a.py", rules).action != "deny"
    assert evaluate("task", "x", rules).action == "deny"


def test_a_relative_path_rule_is_matched_in_any_directory_and_never_allowed() -> None:
    deny = from_config({"deny": ["Edit(src/**)", "Read(.env)"]})
    assert evaluate("edit", "/proj/src/a.py", deny).action == "deny"
    assert (
        evaluate("edit", "/elsewhere/src/b.py", deny).action == "deny"
    )  # stricter than the project root, never looser
    assert evaluate("read", "/proj/.env", deny).action == "deny"
    assert evaluate("edit", "/proj/lib/c.py", deny).action != "deny"
    problems: list[str] = []
    assert from_config({"allow": ["Edit(src/**)"]}, problems) == [] and "allow entry 1" in problems[0]
    absolute = from_config({"allow": ["Edit(//proj/src/**)", "Read(~/notes/**)"]})
    assert [(r.pattern, r.action) for r in absolute] == [("/proj/src/**", "allow"), (expand("~/notes/**"), "allow")]


def test_a_double_slash_is_the_absolute_path_as_in_claude_code() -> None:
    """`Read(//etc/hostname)` is /etc/hostname; a single slash (`/src/**`) is relative to the settings file there."""
    deny = from_config({"deny": ["Read(//etc/hostname)", "Read(/secrets/**)"]})
    assert [r.pattern for r in deny] == ["/etc/hostname", "/etc/hostname/*", "*/secrets/**"]
    assert evaluate("read", "/etc/hostname", deny).action == "deny"
    assert evaluate("read", "/proj/secrets/a", deny).action == "deny"


def test_a_server_wide_mcp_entry_in_every_spelling_is_a_rule_not_a_deny_of_everything() -> None:
    for spec in ("mcp__srv", "mcp__srv__*"):
        rules = from_config({"deny": [spec]})
        assert [(r.tool, r.action) for r in rules] == [("mcp__srv__*", "deny")]
        assert evaluate("mcp__srv__tool", "*", rules).action == "deny"
        assert evaluate("bash", "ls", rules).action != "deny"


def test_a_bare_prefix_rule_is_every_command() -> None:
    assert parse_specifier("Bash(:*)") == ("bash", "*")


def test_empty_parentheses_name_nothing_so_they_are_not_every_command() -> None:
    assert parse_specifier("Bash()") is None and parse_specifier("Read( )") is None
    problems: list[str] = []
    assert from_config({"allow": ["Bash()"]}, problems) == [] and problems  # dropped: an allow of nothing opens nothing
    assert from_config({"deny": ["Bash()"]}, []) == [Rule("*", "*", "deny")]  # a deny that cannot be read fails closed


def test_a_path_allow_that_starts_with_a_star_is_refused_like_the_other_relative_forms() -> None:
    """`Edit(**/*.py)` would open every .py file in every directory (and /etc/cron.d)."""
    for spec in ("Edit(**/*.py)", "Edit(*.py)", "Read(*/secrets/*)"):
        problems: list[str] = []
        assert from_config({"allow": [spec]}, problems) == [] and "allow entry 1" in problems[0], spec
    assert [r.pattern for r in from_config({"allow": ["Read", "Edit(*)"]})] == ["*", "*"]  # the whole tool is meant
    assert [r.pattern for r in from_config({"deny": ["Edit(**/*.py)"]})] == ["**/*.py"]  # a deny of it is fine as is


def test_a_deny_of_a_directory_name_covers_what_is_inside_it() -> None:
    deny = from_config({"deny": ["Read(secrets)", "Edit(//etc/ssh)"]})
    assert evaluate("read", "/proj/secrets", deny).action == "deny"
    assert evaluate("read", "/proj/secrets/a.txt", deny).action == "deny"
    assert evaluate("edit", "/etc/ssh/sshd_config", deny).action == "deny"
    assert evaluate("read", "/proj/secrets-old/a.txt", deny).action != "deny"  # a different name is not covered


def test_grep_and_glob_rules_reach_the_read_decision_and_web_rules_the_web_tools(tmp_path) -> None:
    """The engine decides grep/glob as `read` and the web tools as `web_fetch` / `web_search`: a deny written for the
    Claude Code name must meet a call of that tool."""
    rules = from_config({"deny": ["Grep(/secret/**)", "WebFetch(domain:evil.example)"]})
    assert (
        decide(
            mode="default", tool="grep", args={"pattern": "x", "path": "/secret/a"}, cwd=tmp_path, user_rules=rules
        ).action
        == "deny"
    )
    web = decide(
        mode="default", tool="web_fetch", args={"url": "https://evil.example/x"}, cwd=tmp_path, user_rules=rules
    )
    assert web.action == "deny"
    ok = decide(
        mode="default", tool="web_fetch", args={"url": "https://good.example/x"}, cwd=tmp_path, user_rules=rules
    )
    assert ok.action != "deny"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("bash", {"command": "git commit -m x"}),
        ("edit", {"path": "a.txt"}),
        ("web_fetch", {"url": "https://example.com/x"}),
        ("mcp__srv__tool", {}),
    ],
)
def test_an_imported_ask_rule_asks_in_default_mode_and_auto_answers_it_as_documented(tmp_path, tool, args) -> None:
    """The README's contract for `auto` is "everything that is not denied runs", and the web tools pin it for their own
    ask rules: an `ask` rule asks in default mode and auto mode answers it (unlike Claude Code, where an ask rule
    always prompts); `deny` is what stops auto. Imported rules follow the same contract."""
    ask = from_config({"ask": ["Bash(git commit:*)", "Edit(/**)", "WebFetch(domain:example.com)", "mcp__srv"]})
    assert decide(mode="default", tool=tool, args=args, cwd=tmp_path, user_rules=ask).action == "ask"
    auto = decide(mode="auto", tool=tool, args=args, cwd=tmp_path, user_rules=ask)
    assert (auto.action, auto.auto_allowed, auto.needs_human) == ("allow", True, False)
    deny = from_config({"deny": ["Bash(git commit:*)", "Edit(/**)", "WebFetch(domain:example.com)", "mcp__srv"]})
    assert decide(mode="auto", tool=tool, args=args, cwd=tmp_path, user_rules=deny).action == "deny"


# --- hook permission decisions


def _hook_printing(payload: dict, event: str = "PreToolUse") -> Hook:
    return Hook(event, f"printf '%s' '{json.dumps(payload)}'")


async def _run(*hooks: Hook) -> userhooks.HookOutcome:
    return await HookRunner(list(hooks)).run("PreToolUse", {"tool_input": {}}, tool_name="bash")


async def test_permission_decision_deny_blocks_with_its_reason() -> None:
    out = await _run(
        _hook_printing({"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no"}})
    )
    assert out.blocked and out.reason == "no"


async def test_permission_decision_allow_approves() -> None:
    out = await _run(_hook_printing({"hookSpecificOutput": {"permissionDecision": "allow"}}))
    assert out.approved and not out.blocked and not out.ask


async def test_permission_decision_ask_asks_and_outweighs_another_hooks_allow() -> None:
    allow = _hook_printing({"hookSpecificOutput": {"permissionDecision": "allow"}})
    ask = _hook_printing({"hookSpecificOutput": {"permissionDecision": "ask"}})
    out = await _run(allow, ask)
    assert out.ask and not out.approved and not out.blocked


async def test_the_older_decision_field_still_works() -> None:
    assert (await _run(_hook_printing({"decision": "block", "reason": "stop"}))).blocked
    assert (await _run(_hook_printing({"decision": "approve"}))).approved


async def test_a_permission_decision_is_read_for_pretooluse_only() -> None:
    out = await HookRunner([_hook_printing({"hookSpecificOutput": {"permissionDecision": "deny"}}, "PostToolUse")]).run(
        "PostToolUse", {}, tool_name="bash"
    )
    assert not out.blocked


def test_the_new_events_are_known() -> None:
    for event in ("SubagentStop", "PreCompact", "Notification", "SessionEnd"):
        assert event in EVENTS
        assert userhooks.parse({event: [{"command": "true"}]}, "user")[0].event == event


# --- in the loop


async def _call(loop, command: str) -> dict:
    return await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": command}))


async def test_a_hook_that_asks_makes_yolo_prompt_for_a_call_it_would_run(tmp_path) -> None:
    loop, ran, _ = _stub_bash_loop(tmp_path, "yolo")
    asked: list[str] = []

    async def deny(name, args, decision):
        asked.append(args["command"])
        return _Answer(False)

    loop.approval_callback = deny
    assert "error" not in await _call(loop, "echo plain") and asked == []  # no hook: yolo runs it
    loop.hooks = HookRunner([_hook_printing({"hookSpecificOutput": {"permissionDecision": "ask"}})])
    result = await _call(loop, "echo plain")
    assert "User denied" in result["error"] and asked == ["echo plain"]
    assert [c for c, _ in ran] == ["echo plain"]  # only the first ran


async def test_a_hook_that_asks_with_nobody_to_ask_refuses(tmp_path) -> None:
    loop, ran, _ = _stub_bash_loop(tmp_path, "yolo")
    loop.hooks = HookRunner([_hook_printing({"hookSpecificOutput": {"permissionDecision": "ask"}})])
    result = await _call(loop, "echo plain")
    assert "requires approval" in result["error"] and ran == []


async def test_a_hook_that_allows_does_not_lift_a_hardline(tmp_path) -> None:
    loop, ran, _ = _stub_bash_loop(tmp_path, "default")
    loop.hooks = HookRunner([_hook_printing({"hookSpecificOutput": {"permissionDecision": "allow"}})])
    result = await _call(loop, "rm -rf /")
    assert "error" in result and ran == []


# --- lifecycle events


def _home_hooks(tmp_path, monkeypatch, event: str, out_name: str) -> str:
    out = tmp_path / out_name
    home = tmp_path.parent / f"{tmp_path.name}-k3home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(f"hooks:\n  {event}:\n    - command: 'cat > {out}'\n")
    return str(out)


async def test_precompact_fires_with_the_trigger_and_the_focus(tmp_path, monkeypatch) -> None:
    server, _ = make_server(tmp_path, monkeypatch, replies=["SUMMARY: ok"])
    out = _home_hooks(tmp_path, monkeypatch, "PreCompact", "pre.json")
    sid = await new_session(server, tmp_path)
    server.sessions.get(sid).messages = _long_transcript("the task")
    assert "Compacted" in (await cmd(server, "/compact the plan", sid))["message"]
    payload = json.loads(Path(out).read_text())
    assert payload["hook_event_name"] == "PreCompact"
    assert payload["trigger"] == "manual" and payload["custom_instructions"] == "the plan"
    await server.close()


async def test_session_end_fires_when_a_session_is_closed(tmp_path, monkeypatch) -> None:
    import asyncio

    server, _ = make_server(tmp_path, monkeypatch)
    out = _home_hooks(tmp_path, monkeypatch, "SessionEnd", "end.json")
    sid = await new_session(server, tmp_path)
    for client in server.clients:
        client.session_id = None  # nobody is on it: it may close
    assert (await server.close_live(sid))["closed"]
    await asyncio.gather(*list(server._side_tasks))
    assert json.loads(Path(out).read_text())["reason"] == "closed"
    await server.close()
