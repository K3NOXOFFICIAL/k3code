"""Argument aliases never change what a safety check sees.

``tools.validate.normalize_arguments`` renames near-miss names (``file_path`` -> ``path``, ``dir`` -> ``path``) before
the permission engine decides. Each alias spelling must be decided exactly as the canonical spelling: the same
arguments reach ``decide()`` (and the file guard), the same decision comes back, in every mode; a spelling that is not
an alias of that tool, or two spellings of one argument, are rejected before anything is decided.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from k3code import paths
from k3code.agent.loop import AgentLoop, ApprovalResult
from k3code.permissions.engine import PermissionMode
from k3code.providers.types import ToolCall
from k3code.router import Router, build_chain

MODES = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.AUTO, PermissionMode.YOLO]


class _NoProvider:
    name = "fake"
    base_url = "https://fake.test"

    async def stream(self, *a: Any, **kw: Any):  # pragma: no cover - never called
        if False:
            yield None

    async def aclose(self) -> None:
        pass


def _loop(proj: Path, mode: PermissionMode) -> tuple[AgentLoop, list[dict[str, Any]], list[str]]:
    loop = AgentLoop(
        Router(build_chain([_NoProvider()], [["m"]]), max_retries=0),
        system_prompt="t",
        permission_mode=mode.value,
        cwd=proj,
    )
    seen: list[dict[str, Any]] = []
    actions: list[str] = []
    decide = loop.permissions.decide

    def recording(tool: str, args: dict[str, Any], **kw: Any):
        seen.append(dict(args))
        dec = decide(tool, args, **kw)
        actions.append(f"{dec.action}|{dec.needs_human}|{dec.always_ask}|{dec.hardline}|{dec.message}")
        return dec

    loop.permissions.decide = recording  # type: ignore[method-assign]
    return loop, seen, actions


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    (p / ".git" / "config").write_text("[core]\n")
    (p / ".env").write_text("TOKEN=s3cret\n")
    (p / "a.py").write_text("x = 1\n")
    return p


async def _run(proj: Path, mode: PermissionMode, tool: str, args: dict[str, Any]):
    loop, seen, actions = _loop(proj, mode)
    for name in (".env", ".git/config", "a.py"):
        loop.file_stamps.record(proj / name)
    call = ToolCall(id="c", name=tool, arguments=dict(args))
    result = await asyncio.wait_for(loop._execute_tool(call), 20)
    assert call.arguments == args, "the raw arguments (transcript, journal) are kept as sent"
    plain = _loop(proj, mode)
    plain_answer = plain[0]._plain_allow(ToolCall(id="c", name=tool, arguments=dict(args)))
    error = str(result.get("error", "")).split(" (arguments normalised:")[0]
    return seen, actions, error, (plain_answer, plain[1], plain[2])


def _cases() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    edit = {"old_string": "[core]", "new_string": "[x]"}
    k3home = str(paths.home() / "trust.json")
    return [
        ("read", {"file_path": ".env"}, {"path": ".env"}),
        ("read", {"filepath": "~/.ssh/id_rsa"}, {"path": "~/.ssh/id_rsa"}),
        ("read", {"file": "/etc/shadow"}, {"path": "/etc/shadow"}),
        ("read", {"filename": "/home/ada/notes.txt"}, {"path": "/home/ada/notes.txt"}),  # outside the roots
        ("grep", {"pattern": "x", "dir": "~/.aws"}, {"pattern": "x", "path": "~/.aws"}),
        ("grep", {"pattern": "x", "directory": "/home/ada"}, {"pattern": "x", "path": "/home/ada"}),
        ("glob", {"pattern": "*", "dir": "~/.aws"}, {"pattern": "*", "path": "~/.aws"}),
        ("glob", {"pattern": "*", "directory": "/etc"}, {"pattern": "*", "path": "/etc"}),
        ("write", {"file_path": ".git/config", "content": "x"}, {"path": ".git/config", "content": "x"}),
        ("write", {"filepath": k3home, "content": "{}"}, {"path": k3home, "content": "{}"}),
        ("write", {"file_path": "/home/ada/x.txt", "content": "x"}, {"path": "/home/ada/x.txt", "content": "x"}),
        ("edit", {"file_path": ".git/config", **edit}, {"path": ".git/config", **edit}),
        ("edit", {"file": k3home, **edit}, {"path": k3home, **edit}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES, ids=[m.value for m in MODES])
@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c[0]}-{next(iter(c[1]))}" if isinstance(c, tuple) else "")
async def test_an_alias_is_decided_exactly_like_the_canonical_name(proj: Path, mode: PermissionMode, case) -> None:
    tool, alias, canonical = case
    a_seen, a_actions, a_error, a_plain = await _run(proj, mode, tool, alias)
    c_seen, c_actions, c_error, c_plain = await _run(proj, mode, tool, canonical)
    assert a_seen and a_seen == c_seen, "decide() saw different arguments"
    assert a_actions == c_actions
    assert a_error == c_error
    assert a_plain == c_plain
    assert "file" not in a_seen[0] and "path" in a_seen[0]
    first = c_actions[0]
    # yolo is the no-prompt mode: it allows (after confirming) a write or an outside path; protected paths stay denied
    if mode != PermissionMode.YOLO and (tool in ("write", "edit") or "/home/ada" in str(canonical.get("path"))):
        assert not first.startswith("allow"), f"a protected or outside path was allowed: {first}"
    if canonical.get("path") in (".env", "~/.ssh/id_rsa", "~/.aws") and tool != "glob":
        assert first.startswith("deny"), first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,words",
    [
        ("read", {"path": ".env", "file_path": "a.py"}, ["'path'", "'file_path'"]),
        ("read", {"file_path": ".env", "filename": "a.py"}, ["'file_path'", "'filename'"]),
        ("grep", {"pattern": "x", "dir": "~/.aws", "directory": "."}, ["'dir'", "'directory'"]),
        ("edit", {"path": "a.py", "old": "x", "search": "y", "new_string": "z"}, ["'old'", "'search'"]),
        ("grep", {"pattern": "x", "file": "/etc/shadow"}, ["unknown argument 'file'", "'path'"]),
        ("glob", {"pattern": "*", "file_path": "~/.ssh"}, ["unknown argument 'file_path'", "'path'"]),
        ("read", {"dir": "~/.aws"}, ["unknown argument 'dir'", "'path'"]),
    ],
)
async def test_two_spellings_or_a_foreign_one_are_rejected_before_any_decision(
    proj: Path, tool: str, args: dict[str, Any], words: list[str]
) -> None:
    for mode in MODES:
        seen, _, error, (plain, plain_seen, _) = await _run(proj, mode, tool, args)
        assert seen == [] and plain_seen == [], "the engine was asked about an ambiguous or foreign spelling"
        assert plain is True  # answered with an error, never run in a parallel batch on its own say
        for word in words:
            assert word in error, error


#: (a cleaned spelling, the plain path it must be decided as), ``{HOME}`` being the test's home. ``~`` is expanded by
#: the cleaning, so its plain form is the absolute one.
_SPELLINGS = [
    ("'.env'", ".env"),
    (".env ", ".env"),
    (".env\n", ".env"),
    ("file://{HOME}/.ssh/id_rsa", "{HOME}/.ssh/id_rsa"),
    ("~/.ssh/id_rsa", "{HOME}/.ssh/id_rsa"),
    ('".git/config"', ".git/config"),
]
_EDIT = {"old_string": "[core]", "new_string": "[x]"}
_SHAPES: dict[str, Any] = {
    "read": ("path", {}),
    "write": ("path", {"content": "x"}),
    "edit": ("path", _EDIT),
    "grep": ("path", {"pattern": "x"}),
    "glob": ("path", {"pattern": "*"}),
    "bash": ("cwd", {"command": "ls"}),
}


async def _run_observed(proj: Path, mode: PermissionMode, tool: str, args: dict[str, Any]):
    """The call through the real ``_execute_tool`` with a recording approval prompt and PreToolUse hook."""
    loop, seen, actions = _loop(proj, mode)
    for name in (".env", ".git/config", "a.py"):
        loop.file_stamps.record(proj / name)
    prompts: list[dict[str, Any]] = []
    hooked: list[dict[str, Any]] = []

    async def approve(name: str, arguments: dict[str, Any], decision: Any) -> ApprovalResult:
        prompts.append(dict(arguments))
        return ApprovalResult(choice="deny")

    run_hooks = loop._run_hooks

    async def recording_hooks(event: str, name: str, payload: dict[str, Any]):
        if event == "PreToolUse":
            hooked.append(dict(payload["tool_input"]))
        return await run_hooks(event, name, payload)

    loop.headless = False  # a person is there: "ask" reaches the approval prompt instead of being refused
    loop.approval_callback = approve
    loop._run_hooks = recording_hooks  # type: ignore[method-assign]
    result = await asyncio.wait_for(loop._execute_tool(ToolCall(id="c", name=tool, arguments=dict(args))), 20)
    return seen, actions, prompts, hooked, str(result.get("error", "")).split(" (arguments normalised:")[0]


def _spelling_cases() -> list[Any]:
    return [
        pytest.param(tool, spelling, plain, id=f"{tool}-{i}")
        for tool in _SHAPES
        for i, (spelling, plain) in enumerate(_SPELLINGS)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES, ids=[m.value for m in MODES])
@pytest.mark.parametrize("tool,spelling,plain", _spelling_cases())
async def test_a_cleaned_path_spelling_is_decided_exactly_like_the_plain_path(
    proj: Path, mode: PermissionMode, tool: str, spelling: str, plain: str
) -> None:
    field, rest = _SHAPES[tool]
    home = os.path.expanduser("~")  # the test's own home (a fixture patches it)
    spelling, plain = spelling.replace("{HOME}", home), plain.replace("{HOME}", home)
    s_seen, s_actions, s_prompts, s_hooked, s_error = await _run_observed(proj, mode, tool, {field: spelling, **rest})
    p_seen, p_actions, p_prompts, p_hooked, p_error = await _run_observed(proj, mode, tool, {field: plain, **rest})
    assert s_seen and s_seen[0][field] == p_seen[0][field], f"decide() saw {s_seen[0][field]!r}, not the plain path"
    assert s_seen == p_seen
    assert s_actions == p_actions
    assert s_prompts == p_prompts, "the approval prompt saw a different spelling"
    assert s_hooked == p_hooked, "a PreToolUse hook saw a different spelling"
    assert s_error == p_error
    for args in (*s_prompts, *s_hooked):
        assert args[field] == p_seen[0][field]


@pytest.mark.asyncio
async def test_the_prompt_and_the_hook_are_reached_and_see_the_cleaned_path(proj: Path) -> None:
    args = {"command": "mkdir made", "cwd": " 'sub'\n"}  # an unlisted command: asks
    _, _, prompts, hooked, error = await _run_observed(proj, PermissionMode.DEFAULT, "bash", args)
    assert prompts == [{**args, "cwd": "sub"}] and hooked == prompts
    assert error.startswith("User denied")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args",
    [("read", {"path": "/home/ada/notes.txt"}), ("bash", {"command": "ls", "cwd": "/home/ada"})],
)
async def test_the_add_dir_hint_reads_the_same_for_the_model_and_the_person(
    proj: Path, tool: str, args: dict[str, Any]
) -> None:
    _, actions, _, _, _ = await _run_observed(proj, PermissionMode.DEFAULT, tool, args)
    path = next(v for k, v in args.items() if k in ("path", "cwd"))
    assert f"(/add-dir {path} adds it to this session)" in actions[0]
    assert "ask the user" not in actions[0]
