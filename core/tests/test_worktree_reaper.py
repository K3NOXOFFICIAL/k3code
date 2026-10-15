"""Orphaned fan-out / sub-agent worktrees and k3/* branches: a worker that died with the process that owned it left
``.k3code/worktrees/<id>`` and ``k3/<id>`` behind forever. A gateway start reaps them and /doctor lists them."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from k3code import doctor
from k3code.config import Settings
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.subagents import worktree as wt
from m1cmd_helpers import git_repo

#: What a fan-out worker's process does before it is killed: take a worktree and leave work in it, uncommitted.
CRASHING_WORKER = """
import asyncio, os, signal, sys
from k3code.subagents import worktree as wt

async def main():
    tree = await wt.create(sys.argv[1], sys.argv[2])
    (tree.path / "half.txt").write_text("work in progress\\n")
    os.kill(os.getpid(), signal.SIGKILL)

asyncio.run(main())
"""


def sh(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env={**os.environ, **(env or {})}
    ).stdout


def crash_worker(repo: Path, child_id: str) -> None:
    done = subprocess.run([sys.executable, "-c", CRASHING_WORKER, str(repo), child_id], capture_output=True)
    assert done.returncode == -9, done.stderr.decode()  # killed by SIGKILL: no finally, no cleanup ran


def branch_with_commit(repo: Path, name: str, days_old: int) -> str:
    """``k3/<name>`` holding one commit made ``days_old`` days ago; returns its sha."""
    sh(repo, "checkout", "-q", "-b", f"k3/{name}")
    (repo / f"{name}.txt").write_text(f"{name}\n")
    sh(repo, "add", "-A")
    date = f"{int(time.time()) - days_old * 86400} +0000"
    sh(repo, "commit", "-qm", name, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    sha = sh(repo, "rev-parse", "HEAD").strip()
    sh(repo, "checkout", "-q", "main")
    return sha


def branches(repo: Path) -> set[str]:
    return set(sh(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/k3/").split())


async def test_a_crashed_workers_checkout_is_reaped_and_its_work_kept_on_its_branch(tmp_path):
    repo = git_repo(tmp_path / "repo")
    crash_worker(repo, "w1")
    leftover = repo / ".k3code" / "worktrees" / "w1"
    assert (leftover / "half.txt").exists() and "k3/w1" in branches(repo)  # what a crash left behind

    reaped = await wt.reap(repo)

    assert reaped is not None and [w.id for w in reaped.worktrees] == ["w1"]
    assert not leftover.exists()
    assert sh(repo, "worktree", "list", "--porcelain").count("worktree ") == 1  # only the main checkout is left
    assert sh(repo, "show", "k3/w1:half.txt") == "work in progress\n"  # saved on the branch, not thrown away
    assert "k3/w1" in branches(repo)  # unmerged work is kept (it is young)


async def test_a_worktree_whose_process_is_alive_is_left_alone(tmp_path):
    repo = git_repo(tmp_path / "repo")
    tree = await wt.create(repo, "live1")
    assert tree is not None
    (tree.path / "wip.txt").write_text("still being written\n")

    reaped = await wt.reap(repo)

    assert reaped is not None and reaped.worktrees == []
    assert (tree.path / "wip.txt").read_text() == "still being written\n"
    assert "k3/live1" in branches(repo)


async def test_a_reused_pid_does_not_keep_a_dead_workers_checkout(tmp_path):
    repo = git_repo(tmp_path / "repo")
    tree = await wt.create(repo, "reuse1")
    assert tree is not None
    owner = wt.owner_file(tree.path)
    owner.write_text(f"{os.getpid()} 1\n")  # our pid, but a start time that is not ours: another process then

    reaped = await wt.reap(repo)

    assert reaped is not None and [w.id for w in reaped.worktrees] == ["reuse1"]
    assert not tree.path.exists()


async def test_finishing_a_worktree_normally_leaves_no_owner_file_behind(tmp_path):
    repo = git_repo(tmp_path / "repo")
    tree = await wt.create(repo, "done1")
    assert tree is not None and wt.owner_file(tree.path).exists()
    await wt.remove(tree, keep_branch=False)
    assert not wt.owner_file(tree.path).exists()


async def test_merged_branches_go_and_stale_unmerged_ones_expire_with_their_tip_recorded(tmp_path):
    repo = git_repo(tmp_path / "repo")
    sh(repo, "branch", "k3/merged")  # already in HEAD: nothing on it to lose
    old_sha = branch_with_commit(repo, "stale", days_old=60)
    branch_with_commit(repo, "young", days_old=2)

    reaped = await wt.reap(repo, expire_days=30)

    assert reaped is not None
    assert reaped.merged == ["k3/merged"]
    assert reaped.expired == [("k3/stale", old_sha)]  # the tip is reported, so `git branch <name> <sha>` brings it back
    assert branches(repo) == {"k3/young"}


async def test_only_k3_worktrees_are_reaped_not_the_users_own_branch_worktrees(tmp_path):
    """/branch --worktree puts a checkout in the same directory; it is live work, not a worker's leftover."""
    repo = git_repo(tmp_path / "repo")
    mine = repo / ".k3code" / "worktrees" / "feature"
    sh(repo, "worktree", "add", str(mine), "-b", "feature")
    lookalike = repo / ".k3code" / "worktrees" / "k3-x"  # /branch k3/x --worktree: the directory is not the id
    sh(repo, "worktree", "add", str(lookalike), "-b", "k3/x")

    reaped = await wt.reap(repo)

    assert reaped is not None and reaped.worktrees == [] and reaped.merged == [] and reaped.expired == []
    assert mine.is_dir() and lookalike.is_dir()
    assert {"feature", "k3/x"} <= set(sh(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())


async def test_a_worker_shaped_checkout_outside_the_repo_is_not_removed(tmp_path):
    """The list of worktrees comes out of .git: an entry that points elsewhere is not the reaper's to remove."""
    repo = git_repo(tmp_path / "repo")
    elsewhere = tmp_path / "other" / ".k3code" / "worktrees" / "z1"
    sh(repo, "worktree", "add", str(elsewhere), "-b", "k3/z1")

    reaped = await wt.reap(repo)

    assert reaped is not None and reaped.worktrees == []
    assert elsewhere.is_dir() and "k3/z1" in branches(repo)


async def test_a_worktree_directory_that_vanished_is_pruned(tmp_path):
    repo = git_repo(tmp_path / "repo")
    tree = await wt.create(repo, "gone1")
    assert tree is not None
    shutil.rmtree(tree.path)
    assert "gone1" in sh(repo, "worktree", "list", "--porcelain")  # git still lists it

    await wt.reap(repo)

    assert "gone1" not in sh(repo, "worktree", "list", "--porcelain")


async def test_reap_outside_a_git_repo_does_nothing(tmp_path):
    assert await wt.reap(tmp_path) is None
    assert await wt.scan(tmp_path) is None


async def test_doctor_lists_what_a_crash_left_and_is_green_after_the_reap(tmp_path):
    repo = git_repo(tmp_path / "repo")
    assert (await doctor.check_worktrees(repo)).status == doctor.OK
    crash_worker(repo, "w2")
    branch_with_commit(repo, "stale", days_old=60)

    check = await doctor.check_worktrees(repo)

    assert check.status == doctor.WARN
    assert "1 orphaned worktree" in check.detail and "1 expired" in check.detail
    assert "worktree remove" in check.fix and "branch -D" in check.fix
    assert (await wt.scan(repo)) is not None  # listing changes nothing
    assert (repo / ".k3code" / "worktrees" / "w2").is_dir()

    await wt.reap(repo)

    after = await doctor.check_worktrees(repo)
    assert after.status == doctor.OK
    assert "1 unmerged k3/* branch" in after.detail  # w2's saved work stays listed until it is merged or expires


async def test_a_gateway_start_reaps_the_repos_its_sessions_worked_in(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    repo = git_repo(tmp_path / "repo")
    crash_worker(repo, "w3")
    server = GatewayServer(config=Settings(), store=SessionStore(tmp_path / "sessions.db"))
    sess = server.store.create(title="fan-out", model="m", cwd=str(repo))
    sess.messages = [{"role": "user", "content": "do the big thing"}]  # worked_in skips sessions with no message
    server.store.save(sess)

    await server.reap_orphan_worktrees()

    assert not (repo / ".k3code" / "worktrees" / "w3").exists()
    assert sh(repo, "show", "k3/w3:half.txt") == "work in progress\n"


def planted_worktrees_link(repo: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    """``.git/worktrees`` as a link to a folder outside the repository, holding a stale-looking admin entry (what
    ``git worktree prune`` removes) and a plain file, both harmless markers."""
    victim = tmp_path / "victim"
    stale = victim / "stale"
    stale.mkdir(parents=True)
    (stale / "gitdir").write_text(str(tmp_path / "gone" / ".git") + "\n")
    plain = victim / "plain.txt"
    plain.write_text("keep\n")
    (repo / ".git" / "worktrees").symlink_to(victim, target_is_directory=True)
    return victim, stale, plain


async def test_reap_does_not_prune_through_a_worktrees_link(tmp_path):
    """A link planted at ``.git/worktrees`` (by a sandboxed command, a killed turn whose scrub never ran) would make
    the host-side ``git worktree prune`` of the start-up reap empty the folder it points to."""
    repo = git_repo(tmp_path / "repo")
    victim, stale, plain = planted_worktrees_link(repo, tmp_path)
    await wt.reap(repo)
    assert plain.read_text() == "keep\n" and (stale / "gitdir").is_file()


async def test_reap_still_prunes_a_vanished_worktree_when_worktrees_is_a_real_folder(tmp_path):
    repo = git_repo(tmp_path / "repo")
    sh(repo, "worktree", "add", "-q", str(tmp_path / "other"), "-b", "other")
    shutil.rmtree(tmp_path / "other")
    await wt.reap(repo)
    assert not (repo / ".git" / "worktrees" / "other").exists()


async def test_create_refuses_to_add_a_worktree_through_a_worktrees_link(tmp_path):
    repo = git_repo(tmp_path / "repo")
    victim, stale, plain = planted_worktrees_link(repo, tmp_path)
    before = sorted(p.name for p in victim.iterdir())
    with pytest.raises(RuntimeError, match="symlink"):
        await wt.create(repo, "child1")
    assert sorted(p.name for p in victim.iterdir()) == before
