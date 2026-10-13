# Vendored from hermes-agent@4127d78da84b1eee105f298979cc57cc7457f98d:tools/subagent_worktree.py (MIT)
# Design port, rewritten (async, k3/<id> branches); no code copied verbatim. See VENDOR.toml.
"""Git worktree isolation for sub-agents: one worktree + branch per child, diff back, merge or leave the branch."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from k3code.reliability.sandbox import child_env, harness_git_argv

logger = logging.getLogger("k3code.worktree")

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_CONFIG_NOSYSTEM": "1"}


async def git(cwd: str | Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    """Run git non-interactively; returns (returncode, stdout+stderr). Never raises on a non-zero exit."""
    # harness git: hooks and fsmonitor disabled, scrubbed env (no provider keys reach git)
    proc = await asyncio.create_subprocess_exec(
        *harness_git_argv(*args),
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=child_env(_GIT_ENV),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"git {' '.join(args)} timed out"
    except asyncio.CancelledError:
        proc.kill()  # /stop mid-git: the process must not outlive the turn (a half-done merge, a held index lock)
        await asyncio.shield(proc.wait())
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")


#: One lock per repository checkout: every merge touches the one shared working tree and index, so two merges (two
#: finishing children, a fan-out merge and a child's own) must never overlap.
_REPO_LOCKS: dict[str, asyncio.Lock] = {}


def repo_lock(repo: str | Path) -> asyncio.Lock:
    return _REPO_LOCKS.setdefault(str(Path(repo).resolve()), asyncio.Lock())


#: One lock per repository for ``git worktree add`` / ``remove``. Concurrent adds raced on the shared
#: ``.git/worktrees`` admin dirs: one add read a sibling's half-written ``commondir``
#: ("fatal: failed to read .git/worktrees/<id>/commondir"). Kept apart from ``_REPO_LOCKS`` so a worktree op never
#: waits on a merge. Keyed per loop too: an asyncio lock binds to the loop it first waits on.
_WORKTREE_LOCKS: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def worktree_lock(repo: str | Path) -> asyncio.Lock:
    key = str(Path(repo).resolve())
    loop = asyncio.get_running_loop()
    held = _WORKTREE_LOCKS.get(key)
    if held is None or held[0] is not loop:
        held = _WORKTREE_LOCKS[key] = (loop, asyncio.Lock())
    return held[1]


async def merge_in_progress(repo: str | Path) -> bool:
    rc, _ = await git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return rc == 0


_BUSY = "a merge is already in progress in this checkout; it was left untouched"


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


#: A k3/* branch whose work never merged is kept this long (days since its last commit), then the reaper drops it.
BRANCH_EXPIRY_DAYS = 30
OWNER_SUFFIX = ".owner"


def owner_file(path: Path) -> Path:
    """``<id>.owner`` next to a worktree directory: ``<pid> <start time>`` of the process whose worker uses it."""
    return path.with_name(path.name + OWNER_SUFFIX)


def _proc_start(pid: int) -> str:
    """Field 22 (start time) of /proc/<pid>/stat, "" where there is none. The command name (field 2) may hold spaces
    and ")", so the fields are counted from the last ")"."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return ""
    fields = stat.rpartition(b")")[2].split()
    return fields[19].decode() if len(fields) > 19 else ""


def _owner_alive(path: Path) -> bool:
    """The process that created this worktree is still running (a later process with the same pid does not count).
    No owner file (a worktree from before they existed) or an unreadable one means nobody owns it."""
    try:
        pid_text, _, start = owner_file(path).read_text(encoding="utf-8").strip().partition(" ")
        pid = int(pid_text)
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # another user's process: it exists
    return _proc_start(pid) == start


async def create(parent_cwd: str | Path, child_id: str) -> Worktree | None:
    """``git worktree add .k3code/worktrees/<id> -b k3/<id>``; None outside a git repo (the child shares cwd)."""
    root = await repo_root(parent_cwd)
    if root is None:
        return None
    rc, base = await git(root, "rev-parse", "HEAD")
    if rc != 0:  # no commits yet
        return None
    base = base.strip()
    path = root / ".k3code" / "worktrees" / child_id
    branch = f"k3/{child_id}"
    async with worktree_lock(root):  # parallel adds on one repo race on .git/worktrees (see worktree_lock)
        k3 = root / ".k3code"
        k3.mkdir(exist_ok=True)
        gi = k3 / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")  # worktrees/plans/research never show up as untracked
        path.parent.mkdir(exist_ok=True)
        # before the add: a reaper that lists the new worktree must find its owner (see reap)
        owner_file(path).write_text(f"{os.getpid()} {_proc_start(os.getpid())}\n", encoding="utf-8")
        rc, out = await git(root, "worktree", "add", str(path), "-b", branch, base)
    if rc != 0:
        owner_file(path).unlink(missing_ok=True)
        raise RuntimeError(f"git worktree add failed: {out.strip()}")
    return Worktree(child_id, root, path, branch, base)


async def commit_all(wt: Worktree, message: str) -> bool:
    """Commit whatever the child left in its worktree (so the branch carries it). True if a commit was made."""
    await git(wt.path, "add", "-A")
    rc, _ = await git(wt.path, "diff", "--cached", "--quiet")
    if rc == 0:
        return False
    rc, out = await git(
        wt.path, "-c", "user.name=k3code", "-c", "user.email=k3code@localhost", "commit", "-q", "-m", message
    )
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
    """Merge the child's branch into the parent checkout. On conflict: abort and leave the branch.

    Serialized per repo, and refuses to start while another merge is in progress: the unconditional ``merge --abort``
    below used to destroy the *other* merge (the user's own, or a trial another task was testing) and the failure
    was reported as a conflict of this child's branch.
    """
    async with repo_lock(wt.repo):
        if await merge_in_progress(wt.repo):
            return False, _BUSY
        try:
            rc, out = await git(
                wt.repo,
                "-c",
                "user.name=k3code",
                "-c",
                "user.email=k3code@localhost",
                "merge",
                "--no-ff",
                "-m",
                f"Merge {wt.branch}",
                wt.branch,
            )
        except asyncio.CancelledError:
            await asyncio.shield(git(wt.repo, "merge", "--abort"))  # ours: leave the checkout as we found it
            raise
        if rc == 0:
            return True, out.strip()
        await git(wt.repo, "merge", "--abort")
        return False, out.strip()


async def remove(wt: Worktree, *, keep_branch: bool) -> None:
    """Drop the worktree directory; delete the branch unless it must survive (conflict)."""
    async with worktree_lock(wt.repo):  # a remove rewrites .git/worktrees while a sibling add may be reading it
        await git(wt.repo, "worktree", "remove", "--force", str(wt.path))
        if not keep_branch:
            await git(wt.repo, "branch", "-D", wt.branch)
        owner_file(wt.path).unlink(missing_ok=True)


async def head_sha(repo: str | Path) -> str:
    _, out = await git(repo, "rev-parse", "HEAD")
    return out.strip()


async def merge_trial(wt: Worktree) -> tuple[bool, str]:
    """Merge the branch into the parent checkout WITHOUT committing, so tests can run on the result.

    On conflict the merge is aborted. Follow with :func:`commit_merge` or :func:`abort_merge`. While a trial is
    open (MERGE_HEAD exists) no other merge starts; it is not aborted by anyone but its owner.
    """
    async with repo_lock(wt.repo):
        if await merge_in_progress(wt.repo):
            return False, _BUSY
        try:
            rc, out = await git(wt.repo, "merge", "--no-ff", "--no-commit", wt.branch)
        except asyncio.CancelledError:
            await asyncio.shield(git(wt.repo, "merge", "--abort"))
            raise
        if rc == 0:
            return True, out.strip()
        await git(wt.repo, "merge", "--abort")
        return False, out.strip()


async def commit_merge(wt: Worktree, message: str | None = None) -> tuple[bool, str]:
    rc, out = await git(
        wt.repo,
        "-c",
        "user.name=k3code",
        "-c",
        "user.email=k3code@localhost",
        "commit",
        "-q",
        "--no-edit",
        "-m",
        message or f"Merge {wt.branch}",
    )
    if rc != 0 and "nothing to commit" in out:
        return True, out.strip()  # fast-forward-like no-op merge
    return rc == 0, out.strip()


async def abort_merge(wt: Worktree) -> None:
    await git(wt.repo, "merge", "--abort")


async def bring_parent_into(wt: Worktree, parent_sha: str) -> tuple[bool, str]:
    """Merge the parent's current HEAD into the child's worktree. On conflict the markers stay in the tree
    (the child resolves them); returns (clean, output)."""
    rc, out = await git(
        wt.path, "-c", "user.name=k3code", "-c", "user.email=k3code@localhost", "merge", "--no-edit", parent_sha
    )
    return rc == 0, out.strip()


async def conflicted_files(path: str | Path) -> list[str]:
    _, out = await git(path, "diff", "--name-only", "--diff-filter=U")
    return [x for x in out.splitlines() if x.strip()]


@dataclass
class Leftovers:
    """What fan-out and sub-agent workers left in a repository that no running process owns (:func:`scan` lists it,
    :func:`reap` removes it and returns what it removed)."""

    repo: Path
    worktrees: list[Worktree] = field(default_factory=list)  # k3 worktrees whose process is gone
    merged: list[str] = field(default_factory=list)  # k3/* branches without a worktree whose work is already in HEAD
    expired: list[tuple[str, str]] = field(default_factory=list)  # (branch, tip sha): unmerged, older than the limit
    unmerged: list[str] = field(default_factory=list)  # k3/* branches kept: work not merged yet, not old enough


async def _worktree_list(path: str | Path) -> list[tuple[Path, str]] | None:
    """``(directory, branch ref)`` of every worktree of the repository ``path`` is in, main checkout first."""
    rc, out = await git(path, "worktree", "list", "--porcelain")
    if rc != 0:
        return None
    entries: list[tuple[Path, str]] = []
    for block in out.strip().split("\n\n"):
        fields = dict(line.partition(" ")[::2] for line in block.splitlines())
        if "bare" in fields:
            return None
        if "worktree" in fields:
            entries.append((Path(fields["worktree"]), fields.get("branch", "")))
    return entries or None


def _is_workers_checkout(path: Path, ref: str) -> bool:
    """``.k3code/worktrees/<id>`` on ``k3/<id>``: what :func:`create` makes. A checkout that /branch --worktree made
    sits in the same directory, but on the user's branch, so it never matches."""
    branch = ref.removeprefix("refs/heads/")
    return (
        branch.startswith("k3/")
        and path.name == branch.removeprefix("k3/")
        and path.parent.name == "worktrees"
        and path.parent.parent.name == ".k3code"
    )


async def scan(path: str | Path, *, expire_days: float = BRANCH_EXPIRY_DAYS) -> Leftovers | None:
    """List the orphans in the repository ``path`` is in (None outside one); changes nothing."""
    entries = await _worktree_list(path)
    if entries is None:
        return None
    found = Leftovers(repo=entries[0][0])
    held = {ref.removeprefix("refs/heads/") for _, ref in entries}  # a checked-out branch is in use, whoever owns it
    for tree, ref in entries[1:]:
        # inside the repo: the worktree list is data from .git, and nothing outside this repo is removed on its say-so
        inside = tree.is_dir() and not tree.is_symlink() and tree.resolve().is_relative_to(found.repo.resolve())
        if inside and _is_workers_checkout(tree, ref) and not _owner_alive(tree):
            branch = ref.removeprefix("refs/heads/")
            found.worktrees.append(Worktree(tree.name, found.repo, tree, branch, ""))
    _, merged_out = await git(
        found.repo, "for-each-ref", "--merged=HEAD", "--format=%(refname:short)", "refs/heads/k3/"
    )
    merged = set(merged_out.split())
    _, refs_out = await git(
        found.repo, "for-each-ref", "--format=%(refname:short) %(objectname) %(committerdate:unix)", "refs/heads/k3/"
    )
    now = time.time()
    for line in refs_out.splitlines():
        name, sha, stamp = line.split()
        if name in held:
            continue
        if name in merged:
            found.merged.append(name)
        elif now - int(stamp) > expire_days * 86400:
            found.expired.append((name, sha))
        else:
            found.unmerged.append(name)
    return found


async def reap(path: str | Path, *, expire_days: float = BRANCH_EXPIRY_DAYS) -> Leftovers | None:
    """Remove what :func:`scan` finds in the repository ``path`` is in; returns what was removed (None outside a repo).

    A worker's process dying (a crash, a kill, a reboot) skips the cleanup that follows its turn, so its checkout
    and branch stayed forever. The checkout goes, but what it held is committed to its branch first, and a branch
    with work on it is kept until it is ``expire_days`` old; the tip of an expired one is returned (and logged) so
    ``git branch <name> <sha>`` brings it back. A branch already merged into HEAD has nothing to lose.
    """
    found = await scan(path, expire_days=expire_days)
    if found is None:
        return None
    done: list[Worktree] = []
    for tree in found.worktrees:
        try:
            await commit_all(tree, f"k3code: what {tree.id} left when its process died")
            await remove(tree, keep_branch=True)
        except Exception:  # noqa: BLE001 - one stuck checkout must not keep the rest from being reaped
            logger.warning("could not reap the worktree %s", tree.path, exc_info=True)
            continue
        logger.info("reaped the worktree of %s (its work is on %s)", tree.id, tree.branch)
        done.append(tree)
    # under the lock a session in this process cannot be between `git worktree add`'s branch and its checkout
    async with worktree_lock(found.repo):
        await git(found.repo, "worktree", "prune")  # entries whose directory is gone
        rest = await scan(path, expire_days=expire_days)  # the reaped workers' branches are plain branches now
        if rest is None:
            return found
        for name in rest.merged:
            await git(found.repo, "branch", "-d", name)  # -d: git itself refuses a branch that is not merged
        for name, sha in rest.expired:
            logger.info("expired the branch %s (tip %s)", name, sha)
            await git(found.repo, "branch", "-D", name)
    rest.worktrees = done
    return rest
