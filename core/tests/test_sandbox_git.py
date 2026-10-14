"""``sandbox.git_writable`` (issue #101): off by default, the git metadata is read-only in the sandbox. Turned on,
sandboxed bash can ``git add``/``commit``/``branch``, but the parts of the git metadata git runs as code (hooks,
config, ``info/``) stay read-only, and so do stand-ins for the ones that were missing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from k3code.reliability import sandbox
from k3code.tools import tool_bash

BWRAP = "/usr/bin/bwrap"
needs_bwrap = pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # no GIT_* from the environment (a suite run from a git hook would write into the outer repository)
    return subprocess.run(
        ["git", *args], cwd=cwd, env=sandbox.harness_git_env(), check=True, capture_output=True, text=True
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    assert (path / ".git").is_dir()
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return path.resolve()


def _ro_binds(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]


#: Writes the sandbox must refuse: a hook, the repository config (by hand and through git), info/attributes.
_PLANT = (
    "echo 'echo pwned' > .git/hooks/pre-commit; echo '[core]' >> .git/config; "
    "git config core.hooksPath /tmp; echo '* filter=x' >> .git/info/attributes"
)
_COMMIT = "echo b > b.txt && git add b.txt && git commit -qm second && git branch feat && git checkout -qb topic"


def test_default_binds_only_the_code_parts_of_git_read_only(tmp_path):
    repo = _repo(tmp_path / "repo")
    argv = sandbox.build_argv(repo, home=tmp_path / "home", bwrap=BWRAP, git_writable=True)
    ro = _ro_binds(argv)
    assert str(repo / ".git") not in ro
    for entry in ("hooks", "config", "info"):
        assert str(repo / ".git" / entry) in ro, entry
    bound = argv.index(str(repo))
    assert all(argv.index(str(repo / ".git" / e)) > bound for e in ("hooks", "config", "info"))  # later mounts win


def test_git_writes_are_off_unless_the_user_config_turns_them_on(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    k3home = tmp_path / "k3home"
    k3home.mkdir()
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    assert sandbox.configured_git_writable() is False  # unset: read-only
    assert str(repo / ".git") in _ro_binds(sandbox.build_argv(repo, home=tmp_path / "home", bwrap=BWRAP))
    (k3home / "config.yaml").write_text(yaml.safe_dump({"sandbox": {"git_writable": True}}))
    assert sandbox.configured_git_writable() is True
    assert str(repo / ".git") not in _ro_binds(sandbox.build_argv(repo, home=tmp_path / "home", bwrap=BWRAP))
    (k3home / "config.yaml").write_text(yaml.safe_dump({"sandbox": {"git_writable": "yes"}}))
    assert sandbox.configured_git_writable() is False  # not a boolean: the default


def test_a_project_config_cannot_turn_git_writes_on_or_off(tmp_path, monkeypatch):
    k3home = tmp_path / "k3home"
    k3home.mkdir()
    (k3home / "config.yaml").write_text(yaml.safe_dump({"sandbox": {"git_writable": False}}))
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    project = tmp_path / "proj"
    (project / ".k3code").mkdir(parents=True)
    (project / ".k3code" / "config.yaml").write_text(yaml.safe_dump({"sandbox": {"git_writable": True}}))
    monkeypatch.chdir(project)
    assert sandbox.configured_git_writable() is False


def test_a_gitdir_pointer_outside_a_worktree_admin_dir_stays_read_only(tmp_path):
    """A planted ``.git`` file must not turn an arbitrary host folder writable."""
    proj = tmp_path / "proj"
    proj.mkdir()
    target = tmp_path / "home" / ".config" / "systemd"
    target.mkdir(parents=True)
    (proj / ".git").write_text(f"gitdir: {target}\n")
    argv = sandbox.build_argv(proj, home=tmp_path / "home", bwrap=BWRAP, git_writable=True)
    assert str(target) in _ro_binds(argv)
    assert not any(a == "--bind" and argv[i + 1] == str(target) for i, a in enumerate(argv))


@needs_bwrap
async def test_git_add_commit_and_branch_work_in_the_sandbox(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = (repo / ".git" / "config").read_bytes()
    argv = sandbox.build_argv(repo, home=tmp_path / "home", git_writable=True)
    res = await tool_bash({"command": _COMMIT}, cwd=repo, sandbox=argv)
    assert res["exit_code"] == 0, res
    assert _git(repo, "log", "-1", "--format=%s", "topic").stdout.strip() == "second"
    assert "feat" in _git(repo, "branch", "--list", "feat").stdout
    planted = await tool_bash({"command": _PLANT}, cwd=repo, sandbox=argv)
    assert planted["stderr"].count("Read-only file system") >= 3, planted
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()
    assert (repo / ".git" / "config").read_bytes() == config
    assert not (repo / ".git" / "info" / "attributes").exists()


@needs_bwrap
async def test_by_default_a_command_cannot_create_a_commondir_a_worktree_config_or_a_hook(tmp_path):
    """With nothing configured, a missing ``commondir`` (git's pointer to the config and hooks it uses) stays
    uncreatable, like ``config.worktree`` and a hook."""
    repo = _repo(tmp_path / "repo")
    argv = sandbox.build_argv(repo, home=tmp_path / "home")
    plant = "echo /tmp/x > .git/commondir; echo x > .git/config.worktree; echo x > .git/hooks/pre-commit"
    planted = await tool_bash({"command": plant}, cwd=repo, sandbox=argv)
    assert planted["stderr"].count("Read-only file system") == 3, planted
    for entry in ("commondir", "config.worktree", "hooks/pre-commit"):
        assert not (repo / ".git" / entry).exists(), entry


@needs_bwrap
async def test_missing_code_entries_stay_uncreatable_when_git_is_writable(tmp_path):
    """Opted in, a hook dir, ``info/``, ``modules/`` or ``config.worktree`` the repository lacks gets an empty stand-in
    the command cannot write, and git still commits."""
    repo = _repo(tmp_path / "repo")
    git = repo / ".git"
    shutil.rmtree(git / "hooks")
    shutil.rmtree(git / "info")
    assert not (git / "modules").exists() and not (git / "config.worktree").exists()
    argv = sandbox.build_argv(repo, home=tmp_path / "home", git_writable=True)
    plant = (
        "echo x > .git/hooks/pre-commit; echo x > .git/config.worktree; mkdir .git/modules/x; "
        "echo '* filter=x' > .git/info/attributes"
    )
    planted = await tool_bash({"command": plant}, cwd=repo, sandbox=argv)
    assert planted["stderr"].count("Read-only file system") == 4, planted
    res = await tool_bash({"command": _COMMIT}, cwd=repo, sandbox=argv)
    assert res["exit_code"] == 0, res
    assert _git(repo, "log", "-1", "--format=%s", "topic").stdout.strip() == "second"
    assert [list((git / d).iterdir()) for d in ("hooks", "info", "modules")] == [[], [], []]
    assert (git / "config.worktree").read_bytes() == b""


@needs_bwrap
async def test_git_writes_fail_when_git_writable_is_off(tmp_path):
    repo = _repo(tmp_path / "repo")
    head = _git(repo, "rev-parse", "HEAD").stdout
    argv = sandbox.build_argv(repo, home=tmp_path / "home", git_writable=False)
    res = await tool_bash({"command": _COMMIT}, cwd=repo, sandbox=argv)
    assert res["exit_code"] != 0 and "Read-only file system" in res["stderr"], res
    assert _git(repo, "rev-parse", "HEAD").stdout == head


@needs_bwrap
async def test_a_linked_worktree_can_commit_but_not_plant_a_hook(tmp_path):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "work")
    wt = wt.resolve()
    common = repo / ".git"
    config, pointer = (common / "config").read_bytes(), (wt / ".git").read_bytes()
    argv = sandbox.build_argv(wt, home=tmp_path / "home", git_writable=True)
    res = await tool_bash({"command": _COMMIT}, cwd=wt, sandbox=argv)
    assert res["exit_code"] == 0, res
    assert _git(repo, "log", "-1", "--format=%s", "topic").stdout.strip() == "second"
    admin = common / "worktrees" / "wt"
    plant = (
        f"echo x > {common}/hooks/pre-commit; echo x >> {common}/config; echo 'gitdir: /elsewhere' > .git; "
        f"echo x > {admin}/config.worktree"
    )
    planted = await tool_bash({"command": plant}, cwd=wt, sandbox=argv)
    assert planted["stderr"].count("Read-only file system") == 4, planted
    assert (admin / "config.worktree").read_bytes() == b""
    assert not (common / "hooks" / "pre-commit").exists()
    assert (common / "config").read_bytes() == config
    assert (wt / ".git").read_bytes() == pointer
