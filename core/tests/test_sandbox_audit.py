"""Sandbox audit: a ``.git`` file the sandboxed command can write must not choose what gets bound, and the sandbox
drops every capability (a uid 0 daemon would otherwise hand the command CAP_SYS_ADMIN and friends)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from k3code.reliability import sandbox

BWRAP = "/usr/bin/bwrap"
real_bwrap = pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path) -> Path:
    from m1cmd_helpers import git_repo

    return git_repo(path).resolve()


def _ro_binds(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]


def _world(tmp_path: Path) -> tuple[Path, Path]:
    """A fake ``$HOME`` holding credential-looking files (fake values), and an empty project beside it."""
    home = tmp_path / "home"
    for d in (".aws", ".gnupg", ".config/gcloud", ".k3code"):
        (home / d).mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("aws_secret_access_key = FAKE0000\n")
    (home / ".npmrc").write_text("//registry/:_authToken=FAKE0000\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    return home, proj


def _run(argv: list[str], command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*argv, "/bin/sh", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=30)


# ── 1. a planted .git file / symlink must not bring $HOME (or anything else) back ──


@pytest.mark.parametrize(
    "target",
    [
        "{home}",
        "/",
        "{home}/.aws",
        "{home}/.gnupg",
        "{home}/.config/gcloud",
        "{home}/.k3code",
        "{tmp}",
        "{tmp}/proj/..",
    ],
)
def test_planted_gitdir_file_binds_nothing_outside_the_checkout(tmp_path, target):
    home, proj = _world(tmp_path)
    (proj / ".git").write_text("not a gitdir line\n")
    baseline = _ro_binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))
    (proj / ".git").write_text("gitdir: " + target.format(home=home, tmp=tmp_path) + "\n")
    argv = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    assert _ro_binds(argv) == baseline  # only the .git file itself, as before


@pytest.mark.parametrize("common", ["{home}", "..", "../..", "../../..", "/", "{tmp}", "{home}/.aws"])
def test_planted_commondir_binds_nothing_outside_the_checkout(tmp_path, common):
    """A gitdir inside the project is the command's to write, and so is the ``commondir`` file in it: ``../..`` would
    bring back the project's parent directory (every sibling project) read-only."""
    home, proj = _world(tmp_path)
    (proj / "admin").mkdir()
    (proj / "admin" / "commondir").write_text(common.format(home=home, tmp=tmp_path))
    (proj / ".git").write_text("not a gitdir line\n")
    baseline = _ro_binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))
    (proj / ".git").write_text("gitdir: admin\n")
    ro = _ro_binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))
    assert ro == [*baseline, str((proj / "admin").resolve())]  # the in-project admin dir, nothing else


def test_planted_commondir_with_a_non_utf8_file_does_not_raise(tmp_path):
    home, proj = _world(tmp_path)
    (proj / "admin").mkdir()
    (proj / "admin" / "commondir").write_bytes(b"\xff\xfe\x00../..")
    (proj / ".git").write_text("gitdir: admin\n")
    assert sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])[0] == BWRAP


@pytest.mark.parametrize("link", ["../home", "{home}", "../home/.aws"])
def test_a_git_symlink_out_of_the_project_binds_nothing(tmp_path, link):
    home, proj = _world(tmp_path)
    baseline = _ro_binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))  # no .git at all
    os.symlink(link.format(home=home), proj / ".git")
    assert _ro_binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])) == baseline


def test_a_git_symlink_inside_the_project_is_bound_at_its_target(tmp_path):
    home, proj = _world(tmp_path)
    (proj / ".git-store").mkdir()
    os.symlink(".git-store", proj / ".git")
    ro = _ro_binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))
    assert str(proj.resolve() / ".git-store") in ro and str(proj / ".git") not in ro


@real_bwrap
@pytest.mark.parametrize("variant", ["gitfile", "symlink", "commondir"])
def test_planted_git_metadata_does_not_expose_home_in_the_real_sandbox(tmp_path, variant):
    home, proj = _world(tmp_path)
    if variant == "gitfile":
        (proj / ".git").write_text(f"gitdir: {home}\n")
    elif variant == "symlink":
        os.symlink("../home", proj / ".git")
    else:
        (proj / "admin").mkdir()
        (proj / "admin" / "commondir").write_text(str(home))
        (proj / ".git").write_text("gitdir: admin\n")
    argv = sandbox.build_argv(proj, home=home, home_readonly=[])
    r = _run(argv, f"cat {home}/.aws/credentials {home}/.npmrc; ls -A {home}", proj)
    assert "FAKE0000" not in r.stdout, (variant, r.stdout, r.stderr)
    assert ".aws" not in r.stdout and ".npmrc" not in r.stdout, (variant, r.stdout, r.stderr)


def test_a_linked_worktree_is_still_git_aware(tmp_path):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")
    wt = wt.resolve()
    ro = _ro_binds(sandbox.build_argv(wt, home=tmp_path / "home", bwrap=BWRAP, home_readonly=[]))
    assert str(wt / ".git") in ro
    assert str(repo / ".git" / "worktrees" / "wt") in ro  # the worktree's admin dir …
    assert str(repo / ".git") in ro  # … and the common dir with the objects


@real_bwrap
def test_a_linked_worktree_sees_git_state_in_the_real_sandbox(tmp_path):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")
    wt = wt.resolve()
    argv = sandbox.build_argv(wt, home=tmp_path / "home", home_readonly=[])
    r = _run(argv, "git log --oneline && git rev-parse --abbrev-ref HEAD", wt)
    assert r.returncode == 0, r.stderr
    assert "init" in r.stdout and "feature" in r.stdout
    hook = repo / ".git" / "hooks" / "pre-commit"
    _run(argv, f"echo 'touch pwned' > {hook}; echo 'touch pwned' > {repo}/.git/worktrees/wt/hooks-x", wt)
    assert not hook.exists() and not (repo / ".git" / "worktrees" / "wt" / "hooks-x").exists()  # still read-only


def test_a_worktree_of_another_checkout_is_not_bound(tmp_path):
    """``gitdir:`` aimed at a real worktree admin dir that belongs to a *different* checkout would leak that repo."""
    repo = _repo(tmp_path / "repo")
    _git(repo, "worktree", "add", "-q", str(tmp_path / "other"), "-b", "feature")
    home, proj = _world(tmp_path)
    (proj / ".git").write_text("not a gitdir line\n")
    baseline = _ro_binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))
    (proj / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'other'}\n")
    assert _ro_binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])) == baseline


def test_a_submodule_is_still_git_aware(tmp_path):
    sup, src = _repo(tmp_path / "super"), _repo(tmp_path / "src")
    _git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "sub")
    sub = (sup / "sub").resolve()
    assert (sub / ".git").is_file()
    ro = _ro_binds(sandbox.build_argv(sub, home=tmp_path / "home", bwrap=BWRAP, home_readonly=[]))
    assert str(sup / ".git" / "modules" / "sub") in ro


@real_bwrap
def test_a_submodule_sees_git_state_in_the_real_sandbox(tmp_path):
    sup, src = _repo(tmp_path / "super"), _repo(tmp_path / "src")
    _git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "sub")
    sub = (sup / "sub").resolve()
    r = _run(sandbox.build_argv(sub, home=tmp_path / "home", home_readonly=[]), "git log --oneline", sub)
    assert r.returncode == 0 and "init" in r.stdout, r.stderr


def test_a_separate_git_dir_has_no_back_link_and_is_not_bound(tmp_path):
    """``git init --separate-git-dir`` leaves nothing that ties the admin dir to the checkout, so it cannot be told
    from a planted ``gitdir:``: the checkout is not git-aware inside the sandbox (the safe failure)."""
    work, admin = tmp_path / "work", tmp_path / "admin.git"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main", f"--separate-git-dir={admin}")
    ro = _ro_binds(sandbox.build_argv(work, home=tmp_path / "home", bwrap=BWRAP, home_readonly=[]))
    assert str(admin.resolve()) not in ro and str(work.resolve() / ".git") in ro


# ── 2. no capabilities inside the sandbox ──


def test_the_argv_drops_every_capability(tmp_path):
    home, proj = _world(tmp_path)
    argv = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    assert argv[argv.index("--cap-drop") + 1] == "ALL"


@real_bwrap
def test_the_sandboxed_process_has_no_effective_capabilities(tmp_path):
    home, proj = _world(tmp_path)
    r = _run(
        sandbox.build_argv(proj, home=home, home_readonly=[]), "grep -E '^Cap(Eff|Prm|Bnd)' /proc/self/status", proj
    )
    assert r.returncode == 0, r.stderr
    caps = dict(line.split(":\t") for line in r.stdout.splitlines())
    assert caps["CapEff"].strip() == "0" * 16 and caps["CapPrm"].strip() == "0" * 16, caps


@real_bwrap
def test_a_root_command_cannot_remount_the_git_dir_writable(tmp_path):
    repo = _repo(tmp_path / "repo")
    hook = repo / ".git" / "hooks" / "pre-commit"
    argv = sandbox.build_argv(repo, home=tmp_path / "home", home_readonly=[])
    _run(argv, f"mount -o remount,rw,bind {repo}/.git {repo}/.git; echo 'touch pwned' > {hook}", repo)
    assert not hook.exists()


@real_bwrap
def test_the_availability_probe_still_passes_with_the_capabilities_dropped():
    sandbox.reset_probe()
    try:
        assert sandbox.usable()
    finally:
        sandbox.reset_probe()
