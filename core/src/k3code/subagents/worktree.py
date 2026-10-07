# Design ported from hermes-agent tools/subagent_worktree.py (MIT), rewritten; see VENDOR.toml.
"""Git worktree isolation for sub-agents: one worktree + branch per child, diff back, merge or leave the branch."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_CONFIG_NOSYSTEM": "1"}


async def git(cwd: str | Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    """Run git non-interactively; returns (returncode, stdout+stderr). Never raises on a non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, **_GIT_ENV},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"git {' '.join(args)} timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def repo_root(path: str | Path) -> Path | None:
    rc, out = await git(path, "rev-parse", "--show-toplevel")
    return Path(out.strip()) if rc == 0 and out.strip() else None


@dataclass
class Worktree:
    id: str
    repo: Path
    path: Path
    branch: str
    base: str  # commit the branch started from


async def create(parent_cwd: str | Path, child_id: str) -> Worktree | None:
    """``git worktree add .k3code/worktrees/<id> -b k3/<id>``; None outside a git repo (the child shares cwd)."""
    root = await repo_root(parent_cwd)
    if root is None:
        return None
    rc, base = await git(root, "rev-parse", "HEAD")
    if rc != 0:  # no commits yet
        return None
    base = base.strip()
    k3 = root / ".k3code"
    k3.mkdir(exist_ok=True)
    gi = k3 / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")  # worktrees/plans/research never show up as untracked
    path = k3 / "worktrees" / child_id
    branch = f"k3/{child_id}"
    rc, out = await git(root, "worktree", "add", str(path), "-b", branch, base)
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {out.strip()}")
    return Worktree(child_id, root, path, branch, base)


async def commit_all(wt: Worktree, message: str) -> bool:
    """Commit whatever the child left in its worktree (so the branch carries it). True if a commit was made."""
    await git(wt.path, "add", "-A")
    rc, _ = await git(wt.path, "diff", "--cached", "--quiet")
    if rc == 0:
        return False
    rc, out = await git(wt.path, "-c", "user.name=k3code", "-c", "user.email=k3code@localhost",
                        "commit", "-q", "-m", message)
    if rc != 0:
        raise RuntimeError(f"commit in worktree failed: {out.strip()}")
    return True


async def diff(wt: Worktree, limit: int = 200_000) -> str:
    _, out = await git(wt.repo, "diff", f"{wt.base}...{wt.branch}")
    return out if len(out) <= limit else out[:limit] + "\n[diff truncated]"


async def diff_stat(wt: Worktree) -> str:
    _, out = await git(wt.repo, "diff", "--stat", f"{wt.base}...{wt.branch}")
    return out.strip()


async def merge(wt: Worktree) -> tuple[bool, str]:
    """Merge the child's branch into the parent checkout. On conflict: abort and leave the branch."""
    rc, out = await git(wt.repo, "-c", "user.name=k3code", "-c", "user.email=k3code@localhost",
                        "merge", "--no-ff", "-m", f"Merge {wt.branch}", wt.branch)
    if rc == 0:
        return True, out.strip()
    await git(wt.repo, "merge", "--abort")
    return False, out.strip()


async def remove(wt: Worktree, *, keep_branch: bool) -> None:
    """Drop the worktree directory; delete the branch unless it must survive (conflict)."""
    await git(wt.repo, "worktree", "remove", "--force", str(wt.path))
    if not keep_branch:
        await git(wt.repo, "branch", "-D", wt.branch)


async def head_sha(repo: str | Path) -> str:
    _, out = await git(repo, "rev-parse", "HEAD")
    return out.strip()


async def merge_trial(wt: Worktree) -> tuple[bool, str]:
    """Merge the branch into the parent checkout WITHOUT committing, so tests can run on the result.

    On conflict the merge is aborted. Follow with :func:`commit_merge` or :func:`abort_merge`.
    """
    rc, out = await git(wt.repo, "merge", "--no-ff", "--no-commit", wt.branch)
    if rc == 0:
        return True, out.strip()
    await git(wt.repo, "merge", "--abort")
    return False, out.strip()


async def commit_merge(wt: Worktree, message: str | None = None) -> tuple[bool, str]:
    rc, out = await git(wt.repo, "-c", "user.name=k3code", "-c", "user.email=k3code@localhost",
                        "commit", "-q", "--no-edit", "-m", message or f"Merge {wt.branch}")
    if rc != 0 and "nothing to commit" in out:
        return True, out.strip()  # fast-forward-like no-op merge
    return rc == 0, out.strip()


async def abort_merge(wt: Worktree) -> None:
    await git(wt.repo, "merge", "--abort")


async def bring_parent_into(wt: Worktree, parent_sha: str) -> tuple[bool, str]:
    """Merge the parent's current HEAD into the child's worktree. On conflict the markers stay in the tree
    (the child resolves them); returns (clean, output)."""
    rc, out = await git(wt.path, "-c", "user.name=k3code", "-c", "user.email=k3code@localhost",
                        "merge", "--no-edit", parent_sha)
    return rc == 0, out.strip()


async def conflicted_files(path: str | Path) -> list[str]:
    _, out = await git(path, "diff", "--name-only", "--diff-filter=U")
    return [x for x in out.splitlines() if x.strip()]
