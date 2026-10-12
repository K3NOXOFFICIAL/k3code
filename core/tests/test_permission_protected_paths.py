"""Files the edit tools never change without a person, and the harness's own git.

``write`` and ``edit`` run in the daemon, outside the sandbox, and so does the harness's git (scope classification,
worktrees, triggers). A git hook, config, attributes file, ``.envrc``, husky hook, shell startup file, ssh key or k3code
config planted by the edit tools runs outside the sandbox, so editing one always asks, in every mode and whatever the
rules say; headless, background and sub-agent runs are refused. The harness's git reads no system or global config and
its diffs run no external diff or textconv program.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from k3code.agent.loop import AgentLoop, ApprovalResult
from k3code.permissions import Rule, decide, engine
from k3code.providers.types import ToolCall
from k3code.reliability import sandbox
from k3code.subagents import worktree
from m1cmd_helpers import git_repo

MODES = ["default", "accept-edits", "auto", "yolo"]


def _home() -> Path:
    """The per-test home directory (the conftest points HOME at a fresh path without creating it)."""
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    return home


PROTECTED = [
    ".git/config",
    ".git/config.worktree",
    ".git/hooks/pre-commit",
    ".git/info/attributes",
    ".git/info/exclude",
    ".git/modules/lib/config",
    ".git/modules/lib/hooks/pre-commit",
    ".git/worktrees/w1/config.worktree",
    ".git/worktrees/w1/info/exclude",
    ".git/worktrees/w1/commondir",
    ".git",  # the directory, or the FILE that points a linked worktree at its git directory
    ".gitattributes",
    "sub/dir/.gitattributes",
    ".gitmodules",
    "sub/.gitmodules",
    ".envrc",
    "pkg/.envrc",
    ".husky/pre-commit",
    ".husky/_/husky.sh",
    "web/.husky/commit-msg",
    ".k3code/config.yaml",
    "sub/.k3code/config.yml",
    "sub/.git/hooks/post-commit",  # a nested repository
    "sub/.git",  # a nested repository's .git file
    # spelled another way
    ".GIT/CONFIG",
    ".Git/Hooks/pre-push",
    ".GITATTRIBUTES",
    ".EnvRC",
    ".HUSKY/pre-commit",
    ".K3Code/Config.yaml",
    "./.git/./config",
    "a/../.git/config",
    "sub/../.gitattributes",
]


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("rel", PROTECTED)
def test_editing_a_protected_file_asks_in_every_mode(tmp_path: Path, tool: str, rel: str) -> None:
    for mode in MODES:
        dec = decide(mode=mode, tool=tool, args={"path": rel}, cwd=tmp_path)
        assert dec.action == "ask" and dec.needs_human and dec.always_ask, (mode, rel, dec)
        assert "runs as code" in (dec.message or "")
        assert not dec.auto_allowed
        # headless (`-p`): nobody can answer
        assert decide(mode=mode, tool=tool, args={"path": rel}, cwd=tmp_path, headless=True).action == "deny"
    assert decide(mode="plan", tool=tool, args={"path": rel}, cwd=tmp_path).action == "deny"
    assert engine.protected_path(str(tmp_path / rel))


def _rule_layers(rule: Rule) -> list[dict]:
    return [{"user_rules": [rule]}, {"project_rules": [rule]}, {"session_rules": [rule]}]


@pytest.mark.parametrize("rel", [".git/config", ".git/hooks/pre-commit", ".gitattributes", ".envrc", ".git"])
@pytest.mark.parametrize("pattern", ["*", "{root}/*", "{root}/.git/*", "{root}/{rel}", "*/{rel}"])
def test_no_allow_rule_turns_the_ask_back_into_an_allow(tmp_path: Path, rel: str, pattern: str) -> None:
    rule = Rule("edit", pattern.format(root=tmp_path, rel=rel), "allow")
    for layers in _rule_layers(rule):
        for mode in MODES:
            dec = decide(mode=mode, tool="write", args={"path": rel}, cwd=tmp_path, **layers)
            assert dec.action == "ask" and dec.always_ask, (mode, layers, dec)


def test_the_allow_rules_above_do_win_the_rule_evaluation(tmp_path: Path) -> None:
    """Control: the rule matches and wins (it would allow an ordinary file); the protection overrides its action."""
    rule = Rule("edit", "*", "allow")
    for layers in _rule_layers(rule):
        dec = decide(mode="default", tool="write", args={"path": "src/a.py"}, cwd=tmp_path, **layers)
        assert dec.action == "allow" and dec.rule is not None and dec.rule.action == "allow"
        dec = decide(mode="default", tool="write", args={"path": ".git/config"}, cwd=tmp_path, **layers)
        assert dec.action == "ask" and dec.rule is not None and dec.rule.action == "allow" and dec.always_ask


def test_a_deny_rule_still_denies_a_protected_file(tmp_path: Path) -> None:
    rules = [Rule("edit", "*", "allow"), Rule("edit", f"{tmp_path}/.git/*", "deny")]
    for mode in MODES:
        dec = decide(mode=mode, tool="write", args={"path": ".git/config"}, cwd=tmp_path, user_rules=rules)
        assert dec.action == "deny", mode


NOT_PROTECTED = [
    "src/app.py",
    "README.md",
    ".gitignore",
    ".github/workflows/ci.yml",
    ".gitkeep",
    "docs/gitattributes.md",
    "src/envrc.py",
    ".env",
    ".k3code/plans/p.md",
    ".k3code/rules/style.md",
    ".bashrc",  # a project file of that name is not the home directory's
    ".ssh/config",
    "notes/.git-notes.txt",
]


@pytest.mark.parametrize("rel", NOT_PROTECTED)
def test_ordinary_files_keep_their_decisions(tmp_path: Path, rel: str) -> None:
    assert not engine.protected_path(str(tmp_path / rel))
    assert decide(mode="default", tool="write", args={"path": rel}, cwd=tmp_path).action == "ask"
    for mode in ("accept-edits", "auto", "yolo"):
        dec = decide(mode=mode, tool="edit", args={"path": rel}, cwd=tmp_path)
        assert dec.action == "allow", (mode, rel)


@pytest.mark.parametrize("rel", [".git/config", ".envrc", ".husky/pre-commit", ".gitattributes"])
def test_reading_a_protected_file_is_unchanged(tmp_path: Path, rel: str) -> None:
    for mode in MODES:
        assert decide(mode=mode, tool="read", args={"path": rel}, cwd=tmp_path).action == "allow"


HOME_PROTECTED = [
    ".bashrc",
    ".zshrc",
    ".zshenv",
    ".profile",
    ".bash_profile",
    ".zprofile",
    ".gitconfig",
    ".config/git/config",
    ".config/git/attributes",
    ".config/fish/config.fish",
    ".ssh/authorized_keys",
    ".ssh/config",
    ".ssh/id_ed25519",
    ".aws/credentials",
    ".gnupg/gpg.conf",
    ".config/k3code/env",
    ".k3code/config.yaml",
    ".BASHRC",
    ".SSH/config",
]


@pytest.mark.parametrize("rel", HOME_PROTECTED)
def test_editing_a_shell_startup_file_or_a_key_in_the_home_directory_asks_in_every_mode(
    tmp_path: Path, rel: str
) -> None:
    home = _home()
    for tool in ("write", "edit"):
        for mode in MODES:  # cwd is the home directory: the file lies inside the project root
            dec = decide(mode=mode, tool=tool, args={"path": rel}, cwd=home)
            assert dec.action == "ask" and dec.always_ask and dec.needs_human, (mode, rel, dec)
        broad = Rule("edit", "*", "allow")
        assert decide(mode="yolo", tool=tool, args={"path": rel}, cwd=home, user_rules=[broad]).action == "ask"
    assert engine.protected_path(str(home / rel))


def test_other_files_in_the_home_directory_are_not_protected() -> None:
    home = _home()
    for rel in ("notes.txt", ".bashrc.bak", "proj/.bashrc", "proj/.zshrc", ".config/other/x", ".cache/x"):
        dec = decide(mode="auto", tool="write", args={"path": rel}, cwd=home)
        assert dec.action == "allow", rel


def test_outside_the_project_a_protected_file_still_asks_or_is_denied(tmp_path: Path) -> None:
    home = _home()
    proj = tmp_path / "proj"
    proj.mkdir()
    args = {"path": str(home / ".bashrc")}
    assert decide(mode="default", tool="write", args=args, cwd=proj).action == "ask"
    assert decide(mode="auto", tool="write", args=args, cwd=proj).action == "deny"  # unchanged: outside the roots
    assert decide(mode="yolo", tool="write", args=args, cwd=proj).action == "ask"  # yolo does not skip it
    allow = Rule("edit", f"{home}/*", "allow")
    assert decide(mode="auto", tool="write", args=args, cwd=proj, user_rules=[allow]).action == "ask"


def _symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - no symlinks here
        pytest.skip("symlinks are not available")


def test_a_symlink_does_not_hide_a_protected_file(tmp_path: Path) -> None:
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text("[core]\n")
    _symlink(tmp_path / "docs" / "cfg.txt", tmp_path / ".git" / "config")
    _symlink(tmp_path / "scripts", tmp_path / ".git" / "hooks")  # a link to a directory
    _symlink(tmp_path / "attrs", tmp_path / "sub" / ".gitattributes")  # a link to a file that does not exist yet
    for mode in MODES:
        for rel in ("docs/cfg.txt", "scripts/pre-commit", "attrs"):
            dec = decide(mode=mode, tool="write", args={"path": rel}, cwd=tmp_path)
            assert dec.action == "ask" and dec.always_ask, (mode, rel)
    # a link in the home directory to a startup file
    home = _home()
    (home / ".bashrc").write_text("# rc\n")
    _symlink(home / "work" / "notes.sh", home / ".bashrc")
    assert decide(mode="auto", tool="edit", args={"path": "work/notes.sh"}, cwd=home).always_ask


def test_a_dot_git_file_of_a_linked_worktree_is_protected(tmp_path: Path) -> None:
    main = tmp_path / "main"
    gitdir = main / ".git" / "worktrees" / "w1"
    gitdir.mkdir(parents=True)
    wt = tmp_path / "w1"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n")
    for tool in ("write", "edit"):
        for mode in MODES:
            # repointing the file picks another git directory, and with it another config
            dec = decide(mode=mode, tool=tool, args={"path": ".git"}, cwd=wt)
            assert dec.action == "ask" and dec.always_ask, (mode, tool)
            assert decide(mode=mode, tool=tool, args={"path": ".git/config"}, cwd=wt).always_ask
            # the admin directory of the worktree, when the parent works on it
            dec = decide(mode=mode, tool=tool, args={"path": str(gitdir / "config.worktree")}, cwd=main)
            assert dec.action == "ask" and dec.always_ask, (mode, tool)
    for layers in _rule_layers(Rule("edit", "*", "allow")):
        assert decide(mode="auto", tool="write", args={"path": ".git"}, cwd=wt, **layers).action == "ask"


class _R:
    chain: list = []


async def test_an_unattended_run_is_denied_a_protected_edit_in_any_mode(tmp_path: Path) -> None:
    """Nobody is there to confirm it: a sub-agent, a goal continuation or a background run gets a denial, not a
    prompt that waits for the next client."""
    for kwargs in ({"unattended": True}, {"background": True}):
        for mode in ("auto", "yolo"):
            loop = AgentLoop(
                _R(),  # type: ignore[arg-type]
                system_prompt="x",
                cwd=tmp_path,
                permission_mode=mode,
                headless=False,
                **kwargs,
            )
            call = ToolCall(id="c1", name="write", arguments={"path": ".git/config", "content": "[core]\n"})
            result = await loop._execute_tool(call)
            assert "Permission denied" in result.get("error", ""), (kwargs, mode, result)
            assert not (tmp_path / ".git" / "config").exists()


def _write_call(content: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="write", arguments={"path": ".gitattributes", "content": content})


async def test_an_interactive_yolo_session_waits_for_a_person_before_a_protected_edit(tmp_path: Path) -> None:
    asked: list[str] = []

    async def deny(tool: str, args: dict, decision) -> ApprovalResult:
        asked.append(decision.message)
        return ApprovalResult("deny")

    async def accept(tool: str, args: dict, decision) -> ApprovalResult:
        return ApprovalResult("once")

    def loop(**kw) -> AgentLoop:
        return AgentLoop(
            _R(),  # type: ignore[arg-type]
            system_prompt="x",
            cwd=tmp_path,
            permission_mode="yolo",
            headless=False,
            **kw,
        )

    result = await loop(approval_callback=deny)._execute_tool(_write_call("* filter=x\n"))
    assert len(asked) == 1 and "runs as code" in asked[0]
    assert "User denied" in result["error"] and not (tmp_path / ".gitattributes").exists()
    # without anyone to ask the write does not happen either
    result = await loop()._execute_tool(_write_call("* filter=x\n", "c2"))
    assert "requires approval" in result["error"] and not (tmp_path / ".gitattributes").exists()
    # a person who says yes gets the write
    result = await loop(approval_callback=accept)._execute_tool(_write_call("*.bin -text\n", "c3"))
    assert result.get("ok") and (tmp_path / ".gitattributes").read_text() == "*.bin -text\n"


# ── harness git ──


def test_harness_git_reads_no_system_or_global_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-omni-secret-333")
    env = sandbox.harness_git_env({"GIT_EDITOR": "true"})
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_EDITOR"] == "true" and "PATH" in env and "OMNIROUTE_API_KEY" not in env
    assert sandbox.harness_git_env({"GIT_CONFIG_GLOBAL": "/x"})["GIT_CONFIG_GLOBAL"] == os.devnull  # not overridable
    argv = sandbox.harness_git_argv("status")
    assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
async def test_worktree_git_ignores_the_users_global_git_config(tmp_path: Path) -> None:
    home = _home()
    (home / ".gitconfig").write_text("[test]\n\tglobalmarker = 1\n")
    repo = git_repo(tmp_path / "repo")
    rc, out = await worktree.git(repo, "config", "--list", "--show-origin")
    assert rc == 0 and "core.bare" in out  # the repository's own config is read
    assert "globalmarker" not in out
    plain = subprocess.run(["git", "config", "--list"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "test.globalmarker=1" in plain, "the control: plain git reads it"


def test_scope_classification_runs_git_with_the_hardened_environment(tmp_path: Path, monkeypatch) -> None:
    from k3code.autonomy import scope

    seen: dict = {}

    def spy(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(scope.subprocess, "run", spy)
    scope.repo_summary(tmp_path)
    assert "core.hooksPath=/dev/null" in seen["argv"] and "core.fsmonitor=false" in seen["argv"]
    assert seen["env"]["GIT_CONFIG_GLOBAL"] == os.devnull and seen["env"]["GIT_CONFIG_NOSYSTEM"] == "1"


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
async def test_git_triggers_run_git_with_the_hardened_environment(tmp_path: Path, monkeypatch) -> None:
    from k3code.automation import triggers

    seen: dict = {}
    real_exec = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        seen["env"] = kwargs["env"]
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    await triggers.git_out(str(tmp_path), "--version")
    assert seen["env"]["GIT_CONFIG_GLOBAL"] == os.devnull and seen["env"]["GIT_CONFIG_NOSYSTEM"] == "1"


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
async def test_harness_diff_runs_neither_a_textconv_nor_an_external_diff_program(tmp_path: Path) -> None:
    """``git diff`` runs ``diff.<driver>.textconv`` and ``diff.external``; the harness reads the plain text."""
    repo = git_repo(tmp_path / "repo")
    marker = tmp_path / "program-ran"
    external = tmp_path / "external.sh"
    external.write_text(f"#!/bin/sh\ntouch {marker}\n")
    external.chmod(0o755)
    (repo / ".gitattributes").write_text("*.py diff=x\n")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("add", "-A")
    git("commit", "-qm", "attributes")
    tree = await worktree.create(repo, "d1")
    assert tree is not None
    (tree.path / "a.py").write_text("x = 2\n")
    assert await worktree.commit_all(tree, "child change")
    span = f"{tree.base}...{tree.branch}"
    for key, value in (("diff.x.textconv", f"touch {marker}; cat"), ("diff.external", str(external))):
        git("config", key, value)
        try:
            git("diff", span)  # the control: plain git runs the program
            assert marker.exists(), f"{key} did not run under plain git: nothing to test here"
            marker.unlink()
            assert "x = 2" in await worktree.diff(tree)
            assert "a.py" in await worktree.diff_stat(tree)
            assert await worktree.conflicted_files(repo) == []
            assert not marker.exists(), f"harness git ran the program of {key}"
        finally:
            git("config", "--unset", key)
