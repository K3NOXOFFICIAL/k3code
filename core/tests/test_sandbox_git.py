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


# ── #101 leftover: a git_writable command can still plant a commondir / worktree admin dir / config.worktree key ──


def _snapshot_scrub(repo: Path):
    """Snapshot before, return a callable that scrubs and gives the notes (what the loop does around a bash call)."""
    snap = sandbox.snapshot_git_writable(repo)
    return lambda: sandbox.scrub_git_writable_changes(snap)


@needs_bwrap
async def test_a_planted_commondir_in_the_main_git_is_removed_after_the_command(tmp_path):
    """The hole git_writable left (``_git_metadata_argv``): the .git root stays writable for git's lock files, so a
    command can create a ``.git/commondir`` that points the next git run's config and hooks elsewhere. The scrub
    removes a commondir that was not there before; the user's metadata (no commondir on a normal repo) is unchanged."""
    repo = _repo(tmp_path / "repo")
    assert not (repo / ".git" / "commondir").exists()
    scrub = _snapshot_scrub(repo)
    argv = sandbox.build_argv(repo, home=tmp_path / "home", git_writable=True)
    # the .git root is writable, so the command succeeds at planting the file
    planted = await tool_bash({"command": "echo /tmp/evil > .git/commondir"}, cwd=repo, sandbox=argv)
    assert planted["exit_code"] == 0, planted
    assert (repo / ".git" / "commondir").exists()
    notes = scrub()
    assert not (repo / ".git" / "commondir").exists()  # removed
    assert any("commondir" in n for n in notes), notes
    # the repo still works under harness git (which is what runs outside the sandbox)
    assert _git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


@needs_bwrap
async def test_a_new_worktree_admin_dir_planted_from_the_sandbox_is_removed(tmp_path):
    """A command can also create a ``.git/worktrees/<x>`` admin dir (its own commondir redirects a later git run). The
    scrub removes admin dirs that appeared; a real worktree's admin dir that existed before is kept."""
    repo = _repo(tmp_path / "repo")
    real_wt = tmp_path / "realwt"
    _git(repo, "worktree", "add", "-q", str(real_wt), "-b", "real")
    before = {d.name for d in (repo / ".git" / "worktrees").iterdir()}
    scrub = _snapshot_scrub(repo)
    argv = sandbox.build_argv(repo, home=tmp_path / "home", git_writable=True)
    plant = (
        "mkdir -p .git/worktrees/evil && echo /tmp/evil > .git/worktrees/evil/commondir "
        "&& echo x > .git/worktrees/evil/gitdir"
    )
    planted = await tool_bash({"command": plant}, cwd=repo, sandbox=argv)
    assert planted["exit_code"] == 0, planted
    assert (repo / ".git" / "worktrees" / "evil").is_dir()
    notes = scrub()
    assert not (repo / ".git" / "worktrees" / "evil").exists()  # the planted one is gone
    assert {d.name for d in (repo / ".git" / "worktrees").iterdir()} == before  # the real one stays
    assert any("evil" in n for n in notes), notes


def test_config_worktree_bind_already_stops_a_sandboxed_write_of_it(tmp_path):
    """``config.worktree`` is one of the GIT_CODE_ENTRIES, so build_argv binds it (or an empty stand-in) read-only:
    a sandboxed command cannot write it in the first place. The scrub below is a second line of defence."""
    repo = _repo(tmp_path / "repo")
    cfgw = repo / ".git" / "config.worktree"
    assert not cfgw.exists()
    argv = sandbox.build_argv(repo, home=tmp_path / "home", bwrap=BWRAP, git_writable=True)
    assert str(cfgw) in _ro_binds(argv)  # bound read-only even though it did not exist (a stand-in was made)
    assert cfgw.exists() and cfgw.read_bytes() == b""


def test_the_scrub_strips_only_code_keys_a_command_added_to_config_worktree(tmp_path):
    """Defence in depth for ``config.worktree``: compared against the snapshot, code keys that appeared after the
    command are stripped, a code key the user already had is kept, and so are harmless keys. Driven at the host level
    because the read-only bind means a sandboxed command never reaches the file."""
    repo = _repo(tmp_path / "repo")
    cfgw = repo / ".git" / "config.worktree"
    _git(repo, "config", "extensions.worktreeConfig", "true")
    # the user's own config.worktree already has one code key (a textconv driver) and a harmless one
    cfgw.write_text('[diff "mine"]\n\ttextconv = my-own\n[user]\n\tname = keep\n')
    snap = sandbox.snapshot_git_writable(repo)
    # a command then appends more code keys and another harmless one
    with cfgw.open("a") as fh:
        fh.write(
            "[core]\n\thooksPath = /tmp/evil\n\tpager = evil\n[include]\n\tpath = /tmp/e\n[user]\n\temail = also-keep\n"
        )
    notes = sandbox.scrub_git_writable_changes(snap)
    text = cfgw.read_text()
    assert "hooksPath" not in text and "pager" not in text and "include" not in text.lower(), text
    assert "my-own" in text  # the user's own code key is kept (it was in the snapshot)
    assert "name = keep" in text and "email = also-keep" in text  # harmless keys untouched
    assert notes and "config.worktree" in notes[0]


def test_a_repo_with_no_git_writable_planting_is_left_untouched(tmp_path):
    """The scrub removes only what a command added: a clean repo (even one with a legitimate commondir in a linked
    worktree) comes back with the same metadata and no notes."""
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "work")
    # snapshot from the linked worktree's cwd: its own admin dir has a commondir, which must NOT be removed
    snap = sandbox.snapshot_git_writable(wt.resolve())
    admin_commondir = (repo / ".git" / "worktrees" / "wt" / "commondir").resolve()
    assert admin_commondir.exists()
    notes = sandbox.scrub_git_writable_changes(snap)
    assert notes == []
    assert admin_commondir.exists()  # the worktree's real commondir is kept


def test_harness_git_disables_hooks_and_fsmonitor(tmp_path):
    """The next git run outside the sandbox is harness git; it must not run a hook or fsmonitor program even if one is
    configured (the second line of defence behind the scrub)."""
    import os

    assert sandbox.harness_git_argv("status")[:5] == [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
    ]
    env = sandbox.harness_git_env()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_code_config_key_matching():
    """The subsection wildcard (``filter..clean``) matches any driver name; a plain key matches exactly."""
    ok = sandbox._is_code_config_key
    assert ok("core.hookspath") and ok("core.fsmonitor") and ok("core.sshcommand")
    assert ok("filter.lfs.clean") and ok("diff.mydrv.textconv") and ok("merge.ours.driver")
    assert ok("include.path") and ok("includeif.gitdir:/.path") and ok("credential.https://x.helper")
    assert not ok("user.name") and not ok("core.editorzzz") and not ok("filter.lfs.required")


@needs_bwrap
async def test_the_agent_loop_scrubs_a_planted_commondir_around_a_git_writable_bash_call(tmp_path, monkeypatch):
    """End to end: a yolo loop with ``sandbox.git_writable`` on runs a bash call that plants a ``.git/commondir``; the
    loop snapshots before and scrubs after, so the file is gone when the call returns (the next git run is safe)."""
    from k3code.providers.types import ToolCall
    from test_token_context_budget import make_loop
    from test_wire_clip import Recorder, text_reply, tool_reply

    k3home = tmp_path / "k3home"
    k3home.mkdir()
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    (k3home / "config.yaml").write_text(yaml.safe_dump({"sandbox": {"git_writable": True}}))
    assert sandbox.configured_git_writable() is True

    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    call = ToolCall(id="b", name="bash", arguments={"command": "echo /tmp/evil > .git/commondir && echo planted"})
    provider = Recorder([tool_reply(call), text_reply("done")])
    loop = make_loop(repo, provider, window=None)
    async for _ in loop.run("plant it"):
        pass
    result = next(m.content for m in loop.turn_messages if m.role == "tool")
    assert "planted" in result  # the command ran (the .git root is writable)
    assert not (repo / ".git" / "commondir").exists()  # but the loop removed what it planted
