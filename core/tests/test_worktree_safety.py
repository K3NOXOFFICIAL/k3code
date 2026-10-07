"""Merges into the user's checkout: serialized per repo, and never destroy a merge someone else started."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from pathlib import Path

from k3code.subagents import worktree as wt
from m1cmd_helpers import git_repo


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


async def child(repo: Path, cid: str, filename: str, content: str) -> wt.Worktree:
    tree = await wt.create(repo, cid)
    assert tree is not None
    (tree.path / filename).write_text(content)
    assert await wt.commit_all(tree, f"child {cid}")
    return tree


async def test_a_second_merge_does_not_abort_a_merge_in_progress(tmp_path):
    """merge() ran an unconditional `git merge --abort` on failure: with another merge in flight (the user's own
    unfinished merge, a trial another task was testing) it destroyed that merge and reported a bogus conflict."""
    repo = git_repo(tmp_path / "repo")
    a = await child(repo, "a1", "a.txt", "from a\n")
    b = await child(repo, "b1", "b.txt", "from b\n")
    ok, _ = await wt.merge_trial(a)  # a trial merge is open: MERGE_HEAD exists, nothing committed yet
    assert ok and await wt.merge_in_progress(repo)
    ok_b, out_b = await wt.merge(b)
    assert ok_b is False and "already in progress" in out_b
    assert await wt.merge_in_progress(repo), "the open merge was aborted by someone else"
    assert (repo / "a.txt").exists()  # the trial's changes are still in the working tree
    done, _ = await wt.commit_merge(a)
    assert done and not await wt.merge_in_progress(repo)
    ok_b, _ = await wt.merge(b)  # once the first one is finished the second lands
    assert ok_b and (repo / "b.txt").read_text() == "from b\n"


async def test_concurrent_merges_are_serialized_and_both_land(tmp_path):
    repo = git_repo(tmp_path / "repo")
    trees = [await child(repo, f"c{i}", f"f{i}.txt", f"{i}\n") for i in range(4)]
    results = await asyncio.gather(*(wt.merge(t) for t in trees))
    assert all(ok for ok, _ in results), results
    assert sorted(p.name for p in repo.glob("f*.txt")) == [f"f{i}.txt" for i in range(4)]
    assert not await wt.merge_in_progress(repo)
    assert sh(repo, "status", "--porcelain").strip() == ""


async def test_cancelled_merge_leaves_the_checkout_clean(tmp_path):
    repo = git_repo(tmp_path / "repo")
    tree = await child(repo, "x1", "x.txt", "x\n")
    async with wt.repo_lock(repo):  # hold the lock so the merge waits, then cancel it while waiting
        task = asyncio.create_task(wt.merge(tree))
        await asyncio.sleep(0.05)
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert not await wt.merge_in_progress(repo)
    assert not (repo / "x.txt").exists()
