"""bubblewrap sandbox for auto/yolo bash and unattended children: read-only system, writable project, hidden ``$HOME``.

Sandboxed: auto/yolo sessions, and every unattended session (background run, loop tick, cron job, goal continuation,
sub-agent) whatever its mode. Inside the sandbox ``$HOME`` is an empty tmpfs (only the cache dirs come back), the
project is writable, and its git metadata is read-only so a command cannot plant a hook. ``sandbox.git_writable: true``
makes the git metadata writable (``git add``/``commit``/``branch`` work) except the parts git runs as code or reads to
pick what it runs (hooks, config, ``info/``: see :data:`GIT_CODE_ENTRIES`). The ``.git`` root stays writable for git's
lock files, so a command there can still plant a ``commondir`` (or a new ``worktrees/<x>`` admin dir, or a code key in
``config.worktree``) that the next git run would follow; the loop undoes exactly that after each ``git_writable`` bash
call (:func:`snapshot_git_writable` / :func:`scrub_git_writable_changes`), and harness git runs with hooks and
fsmonitor off regardless (see :func:`harness_git_argv`), so it is still off by default. The network stays on for
interactive sessions; unattended runs get it only when configured (``autonomy.unattended_network``).

Without ``bwrap`` (or where user namespaces are disabled) an interactive session runs unsandboxed: auto mode asks
before each command only the sandbox made safe, yolo runs it, the session shows a warning and ``/doctor`` reports it
(see ``AgentLoop._bwrap_unusable``). An unattended session is refused instead (:class:`SandboxUnavailable`): it never
falls back.

Deliberate exceptions, not sandboxed: MCP stdio servers (see ``mcpclient.stdio_env``) and harness git, which is not
model-controlled and runs with hooks and fsmonitor disabled (see :func:`harness_git_argv`).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code import paths
from k3code.paths import GATEWAY_ENV_VARS
from k3code.permissions import PermissionMode
from k3code.permissions.engine import sensitive_path, windows_drives

logger = logging.getLogger(__name__)

#: ``$HOME`` entries that stay visible inside the sandbox (rw cache, ro uv-managed pythons).
HOME_CACHE = ".cache"
HOME_UV = ".local/share/uv"
SANDBOXED_MODES = (PermissionMode.AUTO, PermissionMode.YOLO)
#: Environment variables a child process inherits (everything else, in particular API keys, is dropped). Used for
#: the sandboxed command and for every unsandboxed child too (tools, gates, fan-out, git, MCP stdio): see
#: :func:`child_env`.
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "NO_COLOR", "COLORTERM")
#: Harness git (worktrees, merges, triggers, scope classification) runs in the daemon, outside the sandbox, on a
#: repository the model may have written to. It never runs a hook or a fsmonitor program, and it reads no system or
#: global git config (see :data:`HARNESS_GIT_ENV`). Diff callers add :data:`HARNESS_GIT_DIFF_FLAGS`.
#:
#: What this cannot prevent: a filter (``filter.<name>.clean``/``smudge``), a diff or merge driver, a signing program
#: or an include, defined in the repository's own config (``.git/config``, ``.git/config.worktree``) still runs when
#: git touches a file it applies to (``status``, ``add``, ``merge``, ``commit``), because git has no switch that
#: ignores a configured driver by name. That is closed at the source: the write and edit tools always ask before they
#: change those files (see :func:`k3code.permissions.engine.protected_path`) and the sandbox binds the git config,
#: hooks and ``info/`` read-only (see :data:`GIT_CODE_ENTRIES`). It stays open for a repository config changed by
#: anything else (an unsandboxed command someone approved, a process outside k3code) and for a driver the user keeps
#: in the repository on purpose.
HARNESS_GIT_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
#: Environment of harness git: no system or global config (``~/.gitconfig`` could define a driver or an alias too).
#: The cost is that the user's own settings do not apply to it either (``safe.directory``, LFS filters, a global
#: ignore file).
HARNESS_GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
#: ``git diff`` runs ``diff.external`` and the textconv drivers by default; the harness only needs the plain text.
HARNESS_GIT_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv")
#: A failed probe is retried after this many seconds (one slow probe used to disable the sandbox for the whole
#: daemon lifetime: every unattended session then ran bash on the real $HOME).
REPROBE_AFTER_S = 60.0
_GITDIR_LINE = re.compile(r"^gitdir:\s*(.+?)\s*$", re.MULTILINE)
#: Files the C library reads to resolve a host name. Any of them may be a link out of the bound system dirs (WSL 2:
#: /etc/resolv.conf -> /mnt/wsl/resolv.conf; Debian resolvconf: -> ../run/resolvconf/resolv.conf): see
#: :func:`_name_lookup_argv`.
NAME_LOOKUP_FILES = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/host.conf", "/etc/gai.conf")
_MAX_LINKS = 40  # the kernel's limit before ELOOP


class SandboxRefused(RuntimeError):
    """The sandbox cannot be built for this request: a writable root that must never be exposed (``$HOME``, a
    directory above it, ``~/.ssh``, the k3code home)."""


class SandboxUnavailable(SandboxRefused):
    """bwrap cannot create the sandbox here. Unattended work is refused, never run unsandboxed."""


#: ``$HOME`` entries masked again after the project binds (a project bind can re-expose them: credentials, keys).
HOME_SECRETS = (".config/k3code", ".ssh")


def configured_git_writable() -> bool:
    """``sandbox.git_writable`` from the user's ``config.yaml`` (never a project's): True lets ``git add``/``commit``/
    ``branch`` write the git metadata in the sandbox (see :func:`_git_metadata_argv` for what stays open). False, the
    git metadata fully read-only, when unset or not a boolean."""
    try:
        from k3code import config

        section = config.load_user_section("sandbox")
    except Exception:  # an unreadable config must not break bash: the sandbox then keeps its default view
        logger.warning("sandbox: could not read sandbox.git_writable from the user config", exc_info=True)
        return False
    value = section.get("git_writable") if isinstance(section, dict) else None
    return value if isinstance(value, bool) else False


def configured_home_readonly() -> list[str]:
    """``sandbox.home_readonly`` from the user's ``config.yaml`` (never a project's): ``$HOME`` entries that stay
    visible, read-only, inside the sandbox. Empty when unset or malformed."""
    try:
        from k3code import config

        section = config.load_user_section("sandbox")
    except Exception:  # an unreadable config must not break bash: the sandbox then keeps its default view
        logger.warning("sandbox: could not read sandbox.home_readonly from the user config", exc_info=True)
        return []
    entries = section.get("home_readonly") if isinstance(section, dict) else None
    return [e for e in entries if isinstance(e, str)] if isinstance(entries, list) else []


#: ``$HOME`` entries ``home_readonly`` never exposes, on top of :data:`HOME_SECRETS` (the k3code home is added too).
HOME_READONLY_REFUSED = (".gnupg", ".aws")


def _home_readonly_paths(home: Path, entries: Iterable[object], writable: Iterable[Path] = ()) -> list[Path]:
    """The entries that may be bound read-only: relative to ``$HOME``, present, and neither ``$HOME`` itself, nor
    above or inside a secret (``~/.ssh``, ``~/.gnupg``, ``~/.aws``, the k3code home), nor above a writable root
    (``writable``: the project, added dirs, the cache), which a read-only bind would shadow. Anything else is
    skipped with a warning; an entry that cannot be inspected (name too long, no permission) is skipped too, never
    raised: a sandbox that cannot be built would leave bash running without one."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    guarded = [home / s for s in (*HOME_SECRETS, *HOME_READONLY_REFUSED)] + [paths.home()]
    if xdg:
        guarded.append(Path(xdg) / "k3code")
    try:
        secrets = [g.resolve() for g in guarded]
        roots = [Path(w).resolve() for w in writable]
    except (OSError, ValueError, RuntimeError):
        logger.warning("sandbox: home_readonly ignored (could not resolve the guarded paths)", exc_info=True)
        return []
    out: list[Path] = []
    for raw in entries:
        text = raw.strip() if isinstance(raw, str) else ""
        rel = Path(text) if text else None
        if rel is None or rel.is_absolute() or ".." in rel.parts or rel.parts in ((), (".",)):
            logger.warning("sandbox: ignoring home_readonly entry %r (must be a path below $HOME)", raw)
            continue
        path = home / rel
        try:
            if not path.exists():
                continue
            real = path.resolve()
        except (OSError, ValueError, RuntimeError):
            logger.warning("sandbox: ignoring home_readonly entry %r (cannot be inspected)", raw)
            continue
        if exposes_home(real, home) or any(real.is_relative_to(s) or s.is_relative_to(real) for s in secrets):
            logger.warning("sandbox: ignoring home_readonly entry %r (reaches $HOME or a secrets folder)", raw)
            continue
        if any(r.is_relative_to(real) for r in roots):
            logger.warning("sandbox: ignoring home_readonly entry %r (it contains a writable folder)", raw)
            continue
        out.append(path)
    return out


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def should_sandbox(mode: PermissionMode | str, background: bool, unattended: bool = False) -> bool:
    """auto/yolo permission modes run sandboxed; so does every unattended session (background run, loop tick,
    cron job, goal continuation, sub-agent), whatever its permission mode."""
    return background or unattended or PermissionMode(mode) in SANDBOXED_MODES


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every child process gets: :data:`ENV_ALLOW` from the daemon, plus ``extra``.

    Provider keys (``OMNIROUTE_API_KEY``, ``K3CODE_API_KEY``, ...) are never passed to a child, so a tool, a gate,
    a fan-out worker or a git trigger cannot print them. Nor does a child learn where the daemon's socket is, not even
    from ``extra`` (an MCP server's configured ``env``): only an authenticated client may drive the daemon.
    """
    env = {name: os.environ[name] for name in ENV_ALLOW if name in os.environ}
    env.update(extra or {})
    for name in GATEWAY_ENV_VARS:
        env.pop(name, None)
    return env


def harness_git_argv(*args: str) -> list[str]:
    """``git`` argv for the harness itself (worktrees, merges, triggers, status): hooks and fsmonitor disabled. Run it
    with :func:`harness_git_env`."""
    return ["git", *HARNESS_GIT_CONFIG, *args]


def harness_git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The :func:`child_env` of a harness git process: no system or global git config (see HARNESS_GIT_ENV)."""
    return child_env({**(extra or {}), **HARNESS_GIT_ENV})


def unattended_prefix(cwd: Path | str, add_dirs: Iterable[Path | str] = (), *, network: bool = False) -> list[str]:
    """The bwrap prefix for an unattended child. Raises :class:`SandboxUnavailable` when bwrap is unusable.

    Unattended work (goal gates, fan-out tests, ultra, automation shells, sub-agents) has no human to see a warning,
    so there is no unsandboxed fallback for it. The network is off unless the caller allows it. Callers run this in
    a thread: :func:`usable` may spawn bwrap (up to 10 s).
    """
    if not usable():
        raise SandboxUnavailable(
            "bubblewrap is unusable here (user namespaces blocked?): unattended commands are refused, "
            "not run unsandboxed; see /doctor"
        )
    return build_argv(cwd, add_dirs, network=network)


async def spawn_unattended_shell(
    command: str,
    *,
    cwd: Path | str,
    add_dirs: Sequence[Path | str] = (),
    extra_env: Mapping[str, str] | None = None,
    stdin: int | None = asyncio.subprocess.DEVNULL,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.STDOUT,
) -> asyncio.subprocess.Process:
    """Start ``command`` through ``/bin/sh`` inside bwrap, with the scrubbed :func:`child_env`.

    The single gate for unattended shell commands. Raises :class:`SandboxRefused` before anything is spawned.
    """
    prefix = await asyncio.to_thread(unattended_prefix, cwd, add_dirs)
    return await asyncio.create_subprocess_exec(
        *prefix,
        "/bin/sh",
        "-c",
        command,
        cwd=str(cwd),
        env=child_env(extra_env),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )


def _refuse_root(root: Path, home: Path, drives: Iterable[str] | None = None) -> None:
    """A writable root must not be ``$HOME``, a directory above it, or one above ``~/.ssh`` or the k3code home; nor,
    under WSL, a Windows profile or a directory above one (see :func:`windows_profile_root`)."""
    for guarded in (home, home / ".ssh", paths.home()):
        target = guarded.resolve()
        if root == target or root in target.parents:
            raise SandboxRefused(
                f"refusing to make {root} writable inside the sandbox: it is or contains $HOME, ~/.ssh or the "
                "k3code home; start the session in a project folder"
            )
    if windows_profile_root(root, drives):
        raise SandboxRefused(
            f"refusing to make {root} writable inside the sandbox: it is or contains a Windows user profile (its "
            ".ssh, .aws and AppData with the browsers' saved passwords); start the session in a project folder"
        )


def windows_profile_root(root: Path | str, drives: Iterable[str] | None = None) -> bool:
    """``root`` is a Windows drive WSL mounts (``/mnt/c``), its ``Users`` folder or a profile in it
    (``/mnt/c/Users/<name>``), or a directory above one (``/mnt``, ``/``): the Windows-side ``$HOME``, which the
    k3code shim starts in when a Windows terminal opens in ``C:\\Users\\<name>`` (issue #134). drvfs is
    case-insensitive, so is this. A project below a profile (``/mnt/c/Users/<name>/src/app``) is fine."""
    folded = str(root).lower().rstrip("/")
    for drive in windows_drives() if drives is None else drives:
        drive = drive.lower().rstrip("/")
        if folded == drive or drive.startswith(folded + "/"):
            return True  # the drive itself, or above it
        if folded.startswith(drive + "/"):
            parts = folded[len(drive) + 1 :].split("/")
            if parts[0] in ("users", "documents and settings") and len(parts) <= 2:
                return True
    return False


def root_refusal(cwd: Path | str, add_dirs: Iterable[Path | str] = (), *, home: Path | None = None) -> str | None:
    """Why :func:`build_argv` would refuse these roots (see :func:`_refuse_root`), or None when it would not."""
    home = Path(home or Path.home())
    try:
        for root in [Path(cwd).resolve(), *(Path(d).resolve() for d in add_dirs)]:
            _refuse_root(root, home)
    except SandboxRefused as exc:
        return str(exc)
    return None


def _git_metadata(root: Path) -> list[Path]:
    """The git metadata of a checkout: ``.git`` itself, or for a linked worktree its gitdir and common dir."""
    dot = root / ".git"
    if dot.is_dir():
        return [dot]
    if not dot.is_file():
        return []
    found = [dot]
    try:
        match = _GITDIR_LINE.search(dot.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        match = None
    if match is not None:
        gitdir = (root / match.group(1)).resolve()
        found.append(gitdir)
        common = gitdir / "commondir"
        if common.is_file():
            found.append((gitdir / common.read_text(encoding="utf-8").strip()).resolve())
    return [p for p in found if p.exists()]


#: Entries of a git dir that stay read-only when the rest of it is writable (``sandbox.git_writable``): what git runs
#: as code or reads to decide what it runs. ``hooks``; ``config`` and ``config.worktree`` (drivers, ``core.hooksPath``,
#: aliases); ``info`` (``info/attributes`` picks the drivers); ``commondir`` (where a linked worktree's config and
#: hooks come from); ``modules`` (each submodule's own git dir, hooks and config included). The same entries of every
#: linked worktree's admin dir (``worktrees/<name>/``) too.
GIT_CODE_ENTRIES = ("hooks", "config", "config.worktree", "info", "commondir", "modules")

#: Git config keys that make git run a program (or read one), by lower-case ``section.key`` or, for a key inside a
#: named subsection, ``section..key`` (the subsection name is whatever the repo chose). The post-command scrub
#: (:func:`scrub_git_writable_changes`) strips these from a ``config.worktree`` a ``git_writable`` command planted,
#: so the next harness git outside the sandbox does not run them. It mirrors what the sandbox already denies at the
#: source (the edit tools and the read-only ``GIT_CODE_ENTRIES`` binds); the plain ``config`` cannot be written
#: (it is bound read-only), so only ``config.worktree`` is scrubbed.
GIT_CODE_CONFIG_KEYS = (
    "core.hookspath",
    "core.fsmonitor",
    "core.sshcommand",
    "core.editor",
    "core.pager",
    "core.askpass",
    "core.gitproxy",
    "diff..command",  # diff.<driver>.command
    "diff..textconv",
    "diff.external",
    "filter..clean",  # filter.<name>.clean / .smudge / .process
    "filter..smudge",
    "filter..process",
    "merge..driver",  # merge.<driver>.driver
    "sequence.editor",
    "gpg.program",
    "gpg..program",  # gpg.<format>.program
    "credential.helper",
    "credential..helper",  # credential.<url>.helper
    "ssh.variant",
    "fsmonitor.hookversion",
    "uploadpack.packobjectshook",
)
#: ``include.path`` / ``includeif.*.path`` pull another config file in (which could set any of the above); stripped too.
_GIT_INCLUDE_KEYS = ("include.path", "includeif..path")


#: Stand-ins for the :data:`GIT_CODE_ENTRIES` a git dir lacks, made on the host before the sandbox starts so a
#: read-only bind can cover them and the command cannot create them: an empty directory or file reads as git's default.
#: A common dir gets all of them; a linked worktree's admin dir (it has a ``commondir``) only ``config.worktree``, the
#: one of them git reads there. ``commondir`` has none: git dies on every stand-in (empty file, directory, dangling
#: link), and ``.`` breaks commits with the reftable backend.
_GIT_STANDIN_DIRS = ("hooks", "info", "modules")
_GIT_STANDIN_FILES = ("config", "config.worktree")


def _make_git_standins(gitdir: Path) -> None:
    admin = (gitdir / "commondir").exists() or (gitdir / "commondir").is_symlink()
    made = [(gitdir / name, False) for name in (("config.worktree",) if admin else _GIT_STANDIN_FILES)]
    made += [] if admin else [(gitdir / name, True) for name in _GIT_STANDIN_DIRS]
    for path, is_dir in made:
        try:  # O_EXCL / mkdir: neither follows a link at the path
            if is_dir:
                path.mkdir()
            else:
                path.touch(exist_ok=False)
        except FileExistsError:
            pass
        except OSError:
            logger.warning("sandbox: could not create %s; the command can create it", path, exc_info=True)


def _git_code_entries(gitdir: Path) -> list[Path] | None:
    """The :data:`GIT_CODE_ENTRIES` of ``gitdir`` and of each ``worktrees/<name>/`` below it, missing ones made first
    (:func:`_make_git_standins`), or None when one of them (or a ``worktrees/<name>``) is a symlink: a read-only bind
    lands on the link's target, and the command could replace the link itself."""
    dirs = [gitdir]
    with contextlib.suppress(OSError):
        dirs += sorted(d for d in (gitdir / "worktrees").iterdir() if d.is_dir() or d.is_symlink())
    if any(d.is_symlink() for d in dirs):
        return None
    for d in dirs:
        _make_git_standins(d)
    found = [d / e for d in dirs for e in GIT_CODE_ENTRIES if (d / e).exists() or (d / e).is_symlink()]
    if any(p.is_symlink() for p in found):
        return None
    return found


def _git_metadata_argv(root: Path, roots: Sequence[Path], home: Path, writable: bool) -> list[str]:
    """bwrap ops for the git metadata of ``root`` (bound read-write before this).

    Read-only metadata (``writable`` False) is bound read-only whole. Writable metadata stays as the project bind
    left it; only its :data:`GIT_CODE_ENTRIES` are bound read-only, so ``git add``/``commit``/``branch`` work and a
    command still cannot plant a hook or a driver. A linked worktree's ``.git`` file (the pointer to its git dir)
    stays read-only, and its git dir and common dir, which live outside the checkout, are bound read-write only when
    they are a real worktree admin dir below a real common dir (``<common>/worktrees/<name>`` with a ``HEAD``, next to
    ``<common>/objects``); a pointer anywhere else would hand the command a writable host folder, so that metadata
    stays read-only as before.

    A missing entry is made empty on the host first (:func:`_make_git_standins`), so the command cannot create it.

    One hole this bind cannot close (``sandbox.git_writable`` is off by default partly for it): a ``commondir`` file in
    the main ``.git``. git dies on any stand-in for it, and the ``.git`` root must stay writable for git's lock files
    (``index.lock``, ``HEAD.lock``), so the command can create one and point the next git run's config and hooks
    elsewhere. The loop closes it after the fact: it snapshots the writable git dirs before the command
    (:func:`snapshot_git_writable`) and removes a commondir, a new ``worktrees/<x>`` admin dir or a ``config.worktree``
    code key the command added (:func:`scrub_git_writable_changes`).
    """
    meta = _git_metadata(root)
    if not writable:
        return [op for m in meta for op in ("--ro-bind", str(m), str(m))]
    argv: list[str] = []
    rw: list[Path] = []
    readonly: list[Path] = []
    gitdirs = [m for m in meta if m.is_dir() and not m.is_symlink()]
    readonly += [m for m in meta if m not in gitdirs]  # the worktree's .git file, or a symlinked .git
    outside = [d for d in gitdirs if not any(d.resolve().is_relative_to(r) for r in roots)]
    if outside and not _linked_worktree_dirs(outside, home):
        readonly += outside
    else:
        rw += outside
    for d in sorted(rw, key=lambda p: len(p.parts)):  # the common dir before the worktree admin dir inside it
        argv += ["--bind", str(d), str(d)]
    for d in gitdirs:
        if d not in readonly:
            entries = _git_code_entries(d)
            readonly += [d] if entries is None else entries
    for p in dict.fromkeys(readonly):
        argv += ["--ro-bind", str(p), str(p)]
    return argv


#: What :func:`snapshot_git_writable` records of one git dir, so the scrub can tell what a command added.
@dataclass
class _GitDirSnapshot:
    gitdir: Path
    had_commondir: bool
    worktrees: frozenset[str]  # names of worktrees/<name> admin dirs that existed before the command
    #: {config.worktree path -> the code keys (:func:`_is_code_config_key`) it already held}, so the scrub strips only
    #: keys a command added, never one the user put there. Keyed by the main git dir's and each worktree admin's file.
    config_code_keys: dict[str, frozenset[str]] = field(default_factory=dict)


def _writable_gitdirs(root: Path, roots: Sequence[Path], home: Path) -> list[Path]:
    """The git dirs :func:`_git_metadata_argv` left writable for ``root`` under ``git_writable`` (the main ``.git``,
    and a linked worktree's own admin dir and common dir): the ones a sandboxed command could have written to."""
    meta = _git_metadata(root)
    gitdirs = [m for m in meta if m.is_dir() and not m.is_symlink()]
    inside = [d for d in gitdirs if any(d.resolve().is_relative_to(r) for r in roots)]
    outside = [d for d in gitdirs if d not in inside]
    if outside and _linked_worktree_dirs(outside, home):
        inside += outside  # a real linked worktree admin dir + common dir are writable too
    return inside


def snapshot_git_writable(cwd: Path | str, add_dirs: Iterable[Path | str] = (), *, home: Path | None = None) -> dict:
    """Record, before a ``git_writable`` sandboxed command, what the writable git dirs hold, so
    :func:`scrub_git_writable_changes` can remove only what the command added. Opaque token; never raises."""
    home = Path(home or Path.home())
    roots = [Path(cwd).resolve(), *(Path(d).resolve() for d in add_dirs)]
    snaps: list[_GitDirSnapshot] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            for gitdir in _writable_gitdirs(root, roots, home):
                g = gitdir.resolve()
                if g in seen:
                    continue
                seen.add(g)
                wts = set()
                with contextlib.suppress(OSError):
                    wts = {d.name for d in (g / "worktrees").iterdir() if d.is_dir() or d.is_symlink()}
                snaps.append(
                    _GitDirSnapshot(
                        g,
                        (g / "commondir").exists() or (g / "commondir").is_symlink(),
                        frozenset(wts),
                        _config_code_keys_of(_config_worktree_files(g, wts)),
                    )
                )
        except (OSError, ValueError, RuntimeError):  # a snapshot that cannot be taken just scrubs nothing later
            logger.warning("sandbox: could not snapshot git metadata of %s", root, exc_info=True)
    return {"gitdirs": snaps}


def scrub_git_writable_changes(snapshot: Mapping[str, Any]) -> list[str]:
    """After a ``git_writable`` sandboxed command, undo the one hole git_writable leaves (see :func:`_git_metadata_argv`
    on ``commondir``): a command could create a ``commondir`` in a git dir that lacked one (pointing the next git run's
    config and hooks elsewhere), a new ``worktrees/<name>`` admin dir (same, via its own ``commondir``), or write code
    keys into a writable ``config.worktree``. Compared against ``snapshot``:

    - a ``commondir`` that appeared where there was none is removed;
    - a ``worktrees/<name>`` admin dir that appeared is removed whole;
    - the :data:`GIT_CODE_CONFIG_KEYS` and include keys are stripped from every ``config.worktree`` of a writable git
      dir (the plain ``config`` is bound read-only and cannot be written).

    Returns one human-readable line per thing removed (the caller logs them). Safe because it only *removes* what a
    model-controlled command just planted and never existed in the user's own metadata; it runs outside the sandbox on
    the host paths but touches nothing but these git-internal, code-running entries. Never raises.
    """
    removed: list[str] = []
    for snap in snapshot.get("gitdirs", []) if isinstance(snapshot, Mapping) else []:
        gitdir: Path = snap.gitdir
        try:
            if not snap.had_commondir:
                removed += _remove_planted(gitdir / "commondir", "commondir file", kind="any")
            wt_root = gitdir / "worktrees"
            surviving = set(snap.worktrees)
            with contextlib.suppress(OSError):
                for d in sorted(wt_root.iterdir(), key=lambda p: p.name):
                    if (d.is_dir() or d.is_symlink()) and d.name not in snap.worktrees:
                        removed += _remove_planted(d, f"worktrees/{d.name} admin dir", kind="tree")
            # config.worktree (the main git dir's and each surviving worktree admin's): strip code keys a command
            # added. config.worktree is in GIT_CODE_ENTRIES and bound read-only, so this is a second line of defence;
            # it never touches a code key the user already had (snapshot).
            for cfg in _config_worktree_files(gitdir, surviving):
                before = snap.config_code_keys.get(str(cfg), frozenset())
                removed += _strip_config_code_keys(cfg, keep=before)
        except Exception:  # noqa: BLE001 - a scrub that fails must not break the turn; the ro binds already held
            logger.warning("sandbox: scrubbing git_writable changes in %s failed", gitdir, exc_info=True)
    return removed


def _config_worktree_files(gitdir: Path, worktrees: Iterable[str]) -> list[Path]:
    """The ``config.worktree`` of ``gitdir`` and of each named ``worktrees/<name>`` admin dir under it."""
    files = [gitdir / "config.worktree"]
    files += [gitdir / "worktrees" / name / "config.worktree" for name in worktrees]
    return files


def _config_code_keys_of(files: Iterable[Path]) -> dict[str, frozenset[str]]:
    """{path -> the code keys each ``config.worktree`` already holds}, for files that exist (used by the snapshot)."""
    out: dict[str, frozenset[str]] = {}
    for cfg in files:
        if cfg.is_file() and not cfg.is_symlink():
            out[str(cfg)] = frozenset(k for k in _list_config_keys(cfg) if _is_code_config_key(k))
    return out


def _remove_planted(path: Path, what: str, *, kind: str) -> list[str]:
    """Remove a file/symlink (``kind='any'``) or a whole tree (``kind='tree'``) a command planted; returns a note or
    nothing. Refuses a symlink's target: only the link itself goes."""
    try:
        if path.is_symlink() or kind == "any" and path.is_file():
            path.unlink()
        elif kind == "tree" and path.is_dir():
            shutil.rmtree(path)
        else:
            return []
    except OSError:
        logger.warning("sandbox: could not remove planted %s at %s", what, path, exc_info=True)
        return []
    return [f"removed a {what} a sandboxed command planted in {path.parent}"]


def _list_config_keys(cfg: Path) -> list[str] | None:
    """The config keys of ``cfg`` (lower-case ``--name-only`` names), or None when git cannot parse the file. Reads
    with the harness env (no system/global config, hooks and fsmonitor off), so listing runs nothing."""
    try:
        listed = subprocess.run(
            ["git", *HARNESS_GIT_CONFIG, "config", "--file", str(cfg), "--name-only", "--list"],
            capture_output=True,
            text=True,
            env=child_env(HARNESS_GIT_ENV),
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if listed.returncode != 0:
        return None
    return [line.strip() for line in listed.stdout.splitlines() if line.strip()]


def _strip_config_code_keys(cfg: Path, *, keep: frozenset[str] = frozenset()) -> list[str]:
    """Remove the code-running keys (:data:`GIT_CODE_CONFIG_KEYS`, include keys) a command added to ``cfg`` (a
    ``config.worktree``): every present code key except those in ``keep`` (the ones the snapshot found there before
    the command, i.e. the user's own). Uses ``git config --file --unset-all`` so other settings stay intact; a file
    git cannot parse that held no code key before is deleted whole (a command wrote it, not the user). Returns a note
    per file touched."""
    if not cfg.is_file() or cfg.is_symlink():
        return []
    present = _list_config_keys(cfg)
    if present is None:  # unparsable now
        if keep:  # the user had a real config here before; leave it rather than delete the user's file
            return []
        with contextlib.suppress(OSError):
            cfg.unlink()
            return [f"removed an unparsable config.worktree a sandboxed command left in {cfg.parent}"]
        return []
    env = child_env(HARNESS_GIT_ENV)
    stripped: list[str] = []
    for key in present:
        if _is_code_config_key(key) and key not in keep:
            try:
                subprocess.run(
                    ["git", *HARNESS_GIT_CONFIG, "config", "--file", str(cfg), "--unset-all", key],
                    capture_output=True,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    timeout=10,
                )
                stripped.append(key)
            except (OSError, subprocess.SubprocessError):
                continue
    if stripped:
        return [f"stripped {', '.join(sorted(stripped))} from the config.worktree in {cfg.parent}"]
    return []


def _is_code_config_key(key: str) -> bool:
    """``key`` (a lower-case ``section.key`` or ``section.sub.key`` from ``git config --name-only``) is one of the
    code-running keys. A pattern with ``..`` matches any subsection name (``filter.lfs.clean`` ~ ``filter..clean``)."""
    key = key.lower()
    for pat in (*GIT_CODE_CONFIG_KEYS, *_GIT_INCLUDE_KEYS):
        if ".." in pat:
            head, tail = pat.split("..", 1)
            if key.startswith(head + ".") and key.endswith("." + tail):
                return True
        elif key == pat:
            return True
    return False


def _linked_worktree_dirs(dirs: Sequence[Path], home: Path) -> bool:
    """``dirs`` are a linked worktree's git dir and its common dir (``<common>/worktrees/<name>`` with a ``HEAD``,
    ``<common>/objects``), none of which is a root :func:`_refuse_root` refuses."""
    if len(dirs) != 2:
        return False
    gitdir, common = (d.resolve() for d in dirs)
    if gitdir.parent != common / "worktrees" or not (gitdir / "HEAD").is_file() or not (common / "objects").is_dir():
        return False
    try:
        for d in (gitdir, common):
            _refuse_root(d, home)
    except SandboxRefused:
        return False
    return True


#: Entries of a project root the sandbox shows read-only, like its git hooks and config: the files git runs as code or
#: reads for its drivers (``.gitattributes``, ``.gitmodules``, husky's ``.husky`` hooks), direnv's ``.envrc`` and the
#: project's k3code config (``.k3code/config*``: permission rules and hooks). The set
#: :func:`k3code.permissions.engine.protected_path` keeps the edit tools from; a sandboxed ``npm test`` could plant a
#: ``.husky/pre-commit`` the user's next commit runs otherwise.
PROTECTED_ENTRIES = (".gitattributes", ".gitmodules", ".husky", ".envrc")


def protected_entries(root: Path) -> list[Path]:
    """The existing :data:`PROTECTED_ENTRIES` of ``root`` plus its ``.k3code/config*`` files."""
    found = [root / e for e in PROTECTED_ENTRIES]
    found += sorted((root / ".k3code").glob("config*"))
    return [p for p in found if p.exists()]


def exposes_home(path: Path | str, home: Path | None = None) -> bool:
    """True when binding ``path`` would bring back all of ``$HOME``: it is ``$HOME`` or contains it (``/``)."""
    home = Path(home or Path.home()).resolve()
    return home.is_relative_to(Path(path).resolve())


def _link_walk(path: str) -> tuple[list[tuple[str, str]], str]:
    """Resolve the absolute ``path`` as the kernel does, one component at a time. Returns every link met on the way as
    ``(where, target)``, ``where`` free of links itself, and the path it ends at. Raises ``OSError`` on a loop."""
    links: list[tuple[str, str]] = []
    pending = [p for p in path.split("/") if p][::-1]
    done = "/"
    while pending:
        part = pending.pop()
        if part == "..":
            done = os.path.dirname(done)
        elif part != ".":
            here = os.path.join(done, part)
            if not os.path.islink(here):
                done = here
                continue
            if len(links) >= _MAX_LINKS:
                raise OSError(errno.ELOOP, "too many levels of symbolic links", path)
            target = os.readlink(here)
            links.append((here, target))
            if target.startswith("/"):
                done = "/"
            pending += [p for p in target.split("/") if p][::-1]
    return links, done


def _name_lookup_argv(shown: Sequence[str], hidden: Sequence[Path]) -> list[str]:
    """bwrap ops that keep each :data:`NAME_LOOKUP_FILES` entry readable when it links out of ``shown`` (the system
    dirs already in the sandbox): the links on the way are recreated and the file at the end is bound read-only at its
    own path. A file whose link or bind would land inside or above ``hidden`` ($HOME, the k3code home), or on or above
    a shown dir or /tmp, is left out: the sandbox keeps hiding those, and an op that collides with another mount fails
    the whole sandbox."""
    shown_paths = [Path(s) for s in shown]
    mounted = (*hidden, *shown_paths, Path("/tmp"))

    def clashes(dest: Path) -> bool:
        return any(dest.is_relative_to(h) for h in hidden) or any(m.is_relative_to(dest) for m in mounted)

    out: list[str] = []
    placed: set[str] = set()
    for name in NAME_LOOKUP_FILES:
        try:
            links, end = _link_walk(name)
        except (OSError, ValueError):
            logger.debug("sandbox: cannot follow %s", name, exc_info=True)
            continue
        if not links or not os.path.isfile(end):
            continue
        ops = [("--symlink", target, where) for where, target in links] + [("--ro-bind", end, end)]
        ops = [op for op in ops if not any(Path(op[2]).is_relative_to(s) for s in shown_paths)]
        if any(clashes(Path(op[2])) for op in ops):
            logger.warning("sandbox: %s links to %s, which the sandbox does not show; names may not resolve", name, end)
            continue
        for op in ops:
            if op[2] not in placed:
                placed.add(op[2])
                out += op
    return out


def build_argv(
    cwd: Path | str,
    add_dirs: Iterable[Path | str] = (),
    *,
    home: Path | None = None,
    bwrap: str | None = None,
    network: bool = True,
    home_readonly: Iterable[str] | None = None,
    readonly_home: bool = False,
    git_writable: bool | None = None,
) -> list[str]:
    """The ``bwrap`` argv *prefix*; append the command (e.g. ``/bin/sh -c "..."``).

    ``home_readonly`` lists ``$HOME`` entries that stay visible read-only (default: ``sandbox.home_readonly`` of the
    user's config). It exists for small marker files a tool must be able to read, e.g. ``.myapp``.

    ``git_writable`` (default: ``sandbox.git_writable`` of the user's config, false) lets ``git add``/``commit``/
    ``branch`` write the git metadata; its hooks, config and ``info/`` stay read-only (see :func:`_git_metadata_argv`).
    False binds all of it read-only.

    A project dir that is ``$HOME`` or contains it (``/``) is not bound: that would undo the home tmpfs and hand the
    command ``~/.config/k3code/env`` and ``~/.ssh``. Those two are masked again after the binds in any case.

    Raises :class:`SandboxRefused` when the cwd or an added dir is ``$HOME``, a directory above it, or a directory
    above ``~/.ssh`` or the k3code home.

    ``readonly_home``: the cwd is ``$HOME`` itself (see :func:`readonly_home_ok`). Nothing is writable but ``/tmp``
    and there is no network: ``$HOME`` and the added dirs are bound read-only, with the credential folders and files
    hidden (:data:`HOME_RO_HIDDEN`). For the plain builtin readers of a session started in the home directory (see
    :func:`k3code.permissions.engine.home_read_only`).
    """
    home = Path(home or Path.home())
    roots = [Path(cwd).resolve(), *(Path(d).resolve() for d in add_dirs)]
    if readonly_home and not readonly_home_ok(cwd, add_dirs, home=home):
        raise SandboxRefused(f"a read-only home sandbox needs $HOME as its working directory, not {roots[0]}")
    for root in roots[1:] if readonly_home else roots:
        _refuse_root(root, home)
    argv = [bwrap or bwrap_path() or "bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc"]
    if not network or readonly_home:  # a read-only home view never has the network: nothing it reads leaves the host
        argv.append("--unshare-net")
    # The command gets a minimal environment, not the daemon's: it carries the provider API keys (OMNIROUTE_API_KEY,
    # ANTHROPIC_API_KEY, ...), so any command run by a prompt-injected turn could print them.
    argv.append("--clearenv")
    for name in ENV_ALLOW:
        value = os.environ.get(name)
        if value is not None:
            argv += ["--setenv", name, value]
    # System: read-only. /bin, /lib* are usually symlinks into /usr; recreate them as symlinks.
    shown = ["/proc", "/dev"]
    for path in ("/usr", "/etc"):
        if os.path.isdir(path):
            argv += ["--ro-bind", path, path]
            shown.append(path)
    for path in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):
        if os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
            shown.append(path)
        elif os.path.isdir(path):
            argv += ["--ro-bind", path, path]
            shown.append(path)
    # /etc/resolv.conf is often a link into /run; keep name resolution working (when the network is on).
    for path in ("/run/systemd/resolve", "/run/NetworkManager"):
        if os.path.isdir(path):
            argv += ["--ro-bind", path, path]
            shown.append(path)
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    # A resolver file linked anywhere else (WSL 2: /mnt/wsl/resolv.conf) is bound at its own path, after the /tmp
    # tmpfs and before $HOME is masked; never from $HOME or the k3code home. Both spellings of those count: the later
    # mounts use the given path, and a link recreated above it would make bwrap fail to place them.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    private = [home, paths.home(), *([Path(xdg) / "k3code"] if xdg else [])]
    argv += _name_lookup_argv(shown, [*private, *(Path(os.path.realpath(p)) for p in private)])
    if readonly_home:
        return [*argv, *_readonly_home_argv(home, roots[1:], xdg)]
    # $HOME hidden behind an empty tmpfs, then only the cache dirs and the project are bound back.
    argv += ["--tmpfs", str(home)]
    cache = home / HOME_CACHE
    if cache.is_dir():
        argv += ["--bind", str(cache), str(cache)]
    uv = home / HOME_UV
    if uv.is_dir():
        argv += ["--ro-bind", str(uv), str(uv)]
    if git_writable is None:
        git_writable = configured_git_writable()
    seen: set[str] = set()
    for root in roots:
        p = str(root)
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            if exposes_home(p, home):
                logger.warning("sandbox: not binding %s (it would expose $HOME)", p)
                continue
            argv += ["--bind", p, p]
            # the git metadata is read-only (only its hooks, config and info with sandbox.git_writable true): a
            # sandboxed command cannot plant a hook or change core.* config
            argv += _git_metadata_argv(root, roots, home, git_writable)
            for meta in protected_entries(root):
                argv += ["--ro-bind", str(meta), str(meta)]
    # Listed $HOME entries come back read-only after the project binds, so a project bind cannot make one writable
    extra = configured_home_readonly() if home_readonly is None else list(home_readonly)
    for path in _home_readonly_paths(home, extra, [*roots, cache]):
        argv += ["--ro-bind", str(path), str(path)]
    # $HOME secrets are masked again after the project binds: a bind above them would re-expose them
    secrets = [home / s for s in HOME_SECRETS] + ([Path(xdg) / "k3code"] if xdg else [])
    for path in dict.fromkeys(str(p) for p in secrets):
        if os.path.isdir(path):
            argv += ["--tmpfs", path]
    argv += ["--chdir", str(roots[0])]
    return argv


#: What a read-only ``$HOME`` view hides on top of :data:`HOME_SECRETS`, :data:`HOME_READONLY_REFUSED`, the k3code
#: home and the top-level dotenv files: well-known credential files and folders (tokens, registry logins, keyrings,
#: cloud and agent logins, browser profiles) and shell/REPL histories (pasted tokens, ``export KEY=...``). A dotenv
#: file deeper down cannot be hidden this way: :func:`k3code.permissions.engine.home_read_only` keeps every command
#: that walks a tree or expands a glob out of this sandbox, and an explicitly named one is refused before it runs.
HOME_RO_HIDDEN = (
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".vault-token",
    ".docker",
    ".kube",
    ".azure",
    ".config/gcloud",
    ".config/gh",
    ".config/hub",
    ".config/rclone",
    ".password-store",
    ".local/share/keyrings",
    ".local/share/k3code",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".terraform.d",
    ".claude",
    ".claude.json",
    ".codex",
    ".mozilla",
    ".thunderbird",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".var/app",
    ".bash_history",
    ".zsh_history",
    ".zhistory",
    ".histfile",
    ".sh_history",
    ".local/share/fish",
    ".python_history",
    ".node_repl_history",
    ".psql_history",
    ".mysql_history",
    ".sqlite_history",
    ".rediscli_history",
    ".lesshst",
)


def readonly_home_ok(cwd: Path | str, add_dirs: Iterable[Path | str] = (), *, home: Path | None = None) -> bool:
    """``cwd`` is ``$HOME`` itself and no added dir is refused as a root (see :func:`_refuse_root`): a session a
    read-only home sandbox can serve. A cwd above ``$HOME`` (``/``) is not."""
    home = Path(home or Path.home())
    try:
        if Path(cwd).resolve() != home.resolve():
            return False
        for d in add_dirs:
            _refuse_root(Path(d).resolve(), home)
    except (OSError, SandboxRefused):
        return False
    return True


def _readonly_home_argv(home: Path, add_dirs: Sequence[Path], xdg: str | None) -> list[str]:
    """bwrap ops of :func:`build_argv` with ``readonly_home``: $HOME and the added dirs read-only, secrets hidden."""
    argv = ["--ro-bind", str(home), str(home)]
    # where each host directory shows inside the sandbox: (its realpath, the path it is mounted at)
    shown = [(os.path.realpath(home), str(home))]
    for d in add_dirs:
        if d.is_dir() and not d.is_relative_to(home.resolve()) and not exposes_home(d, home):
            argv += ["--ro-bind", str(d), str(d)]
            shown.append((os.path.realpath(d), str(d)))
    shown += [(os.path.realpath(p), p) for p in ("/usr", "/etc") if os.path.isdir(p)]
    hidden = [home / s for s in (*HOME_SECRETS, *HOME_READONLY_REFUSED, *HOME_RO_HIDDEN)] + [paths.home()]
    if xdg:
        hidden.append(Path(xdg) / "k3code")
    with contextlib.suppress(OSError):
        hidden += [Path(e.path) for e in os.scandir(home) if sensitive_path(e.path)]  # ~/.env, ~/.env.local, ...
    # Hidden where the file really is: a symlinked ~/.netrc (dotfile managers) is masked at its target, which is also
    # what the link reaches inside the sandbox. A target the sandbox does not show is unreachable there anyway.
    masked: set[str] = set()
    for p in hidden:
        real = os.path.realpath(p)
        at = _shown_at(real, shown)
        if at is None or at in masked:
            continue
        if os.path.isdir(real):
            argv += ["--tmpfs", at]
        elif os.path.exists(real):
            argv += ["--ro-bind", os.devnull, at]
        else:
            continue
        masked.add(at)
    return [*argv, "--chdir", str(home)]


def _shown_at(real: str, shown: Sequence[tuple[str, str]]) -> str | None:
    """The path inside the sandbox at which the host path ``real`` (a realpath) shows, or None."""
    for host, inside in sorted(shown, key=lambda s: len(s[0]), reverse=True):
        if real == host:
            return inside
        if real.startswith(host.rstrip("/") + "/"):
            return os.path.join(inside, os.path.relpath(real, host))
    return None


def with_chdir(argv: Sequence[str], cwd: Path | str) -> list[str]:
    """The same bwrap prefix, started in ``cwd`` (a bash call's own cwd). A dir outside the mounted roots does not
    exist inside the sandbox, so the chdir fails there rather than reaching the host."""
    out = list(argv)
    if "--chdir" in out:
        index = out.index("--chdir")
        out[index + 1] = str(Path(cwd).resolve())
    return out


_probe: tuple[bool, float] | None = None  # (result, monotonic time)


def reset_probe() -> None:
    """Forget the cached probe result (tests, ``/doctor``)."""
    global _probe
    _probe = None


def usable() -> bool:
    """True when bwrap exists and can really create the sandbox here (user namespaces enabled).

    A positive result is cached; a negative one is re-probed after ``REPROBE_AFTER_S`` so a transient failure
    (a timeout under IO pressure, EAGAIN) does not latch the sandbox off.
    """
    global _probe
    now = time.monotonic()
    if _probe is not None and (_probe[0] or now - _probe[1] < REPROBE_AFTER_S):
        return _probe[0]
    ok = _probe_bwrap()
    _probe = (ok, now)
    return ok


def _probe_bwrap() -> bool:
    path = bwrap_path()
    if path is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="k3code-bwrap-") as scratch:
            r = subprocess.run(
                [*build_argv(scratch, bwrap=path), "/bin/true"],
                capture_output=True,
                timeout=10,
                check=False,
                env=child_env(),
            )
    except (OSError, subprocess.SubprocessError, SandboxRefused):
        return False
    return r.returncode == 0


def hidden_name_lookup_files() -> list[str]:
    """The :data:`NAME_LOOKUP_FILES` readable here but not inside the sandbox, where host names then do not resolve
    although the network is on. Reads files only, no lookup. Spawns bwrap (up to 10 s); [] when it cannot run."""
    path = bwrap_path()
    wanted = [f for f in NAME_LOOKUP_FILES if os.access(f, os.R_OK)]
    if path is None or not wanted:
        return []
    script = 'for f; do [ -r "$f" ] || echo "$f"; done'
    try:
        with tempfile.TemporaryDirectory(prefix="k3code-bwrap-") as scratch:
            r = subprocess.run(
                [*build_argv(scratch, bwrap=path), "/bin/sh", "-c", script, "sh", *wanted],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=child_env(),
            )
    except (OSError, subprocess.SubprocessError, SandboxRefused):
        return []
    return [f for f in r.stdout.splitlines() if f in wanted] if r.returncode == 0 else []
