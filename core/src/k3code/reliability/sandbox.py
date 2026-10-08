"""bubblewrap sandbox for auto/yolo bash and unattended children: read-only system, writable project, hidden ``$HOME``.

Sandboxed: auto/yolo sessions, and every unattended session (background run, loop tick, cron job, goal continuation,
sub-agent) whatever its mode. Inside the sandbox ``$HOME`` is an empty tmpfs (only the cache dirs come back), the
project is writable, and its git metadata is read-only so a command cannot plant a hook. The network stays on for
interactive sessions; unattended runs get it only when configured (``autonomy.unattended_network``).

Without ``bwrap`` (or where user namespaces are disabled) an interactive session runs unsandboxed and ``/doctor``
warns. An unattended session is refused instead (:class:`SandboxUnavailable`): it never falls back.

Deliberate exceptions, not sandboxed: MCP stdio servers (see ``mcpclient.stdio_env``) and harness git, which is not
model-controlled and runs with hooks and fsmonitor disabled (see :func:`harness_git_argv`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from k3code import paths
from k3code.permissions import PermissionMode

logger = logging.getLogger(__name__)

#: ``$HOME`` entries that stay visible inside the sandbox (rw cache, ro uv-managed pythons).
HOME_CACHE = ".cache"
HOME_UV = ".local/share/uv"
SANDBOXED_MODES = (PermissionMode.AUTO, PermissionMode.YOLO)
#: Environment variables a child process inherits (everything else, in particular API keys, is dropped). Used for
#: the sandboxed command and for every unsandboxed child too (tools, gates, fan-out, git, MCP stdio): see
#: :func:`child_env`.
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "NO_COLOR", "COLORTERM")
#: Harness git never runs a hook or a fsmonitor program: a sandboxed command could have written one into .git.
HARNESS_GIT_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
#: A failed probe is retried after this many seconds (one slow probe used to disable the sandbox for the whole
#: daemon lifetime: every unattended session then ran bash on the real $HOME).
REPROBE_AFTER_S = 60.0
_GITDIR_LINE = re.compile(r"^gitdir:\s*(.+?)\s*$", re.MULTILINE)


class SandboxRefused(RuntimeError):
    """The sandbox cannot be built for this request: a writable root that must never be exposed (``$HOME``, a
    directory above it, ``~/.ssh``, the k3code home)."""


class SandboxUnavailable(SandboxRefused):
    """bwrap cannot create the sandbox here. Unattended work is refused, never run unsandboxed."""


#: ``$HOME`` entries masked again after the project binds (a project bind can re-expose them: credentials, keys).
HOME_SECRETS = (".config/k3code", ".ssh")


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def should_sandbox(mode: PermissionMode | str, background: bool, unattended: bool = False) -> bool:
    """auto/yolo permission modes run sandboxed; so does every unattended session (background run, loop tick,
    cron job, goal continuation, sub-agent), whatever its permission mode."""
    return background or unattended or PermissionMode(mode) in SANDBOXED_MODES


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every child process gets: :data:`ENV_ALLOW` from the daemon, plus ``extra``.

    Provider keys (``OMNIROUTE_API_KEY``, ``K3CODE_API_KEY``, ...) are never passed to a child, so a tool, a gate,
    a fan-out worker or a git trigger cannot print them.
    """
    env = {name: os.environ[name] for name in ENV_ALLOW if name in os.environ}
    env.update(extra or {})
    return env


def harness_git_argv(*args: str) -> list[str]:
    """``git`` argv for the harness itself (worktrees, merges, triggers, status): hooks and fsmonitor disabled."""
    return ["git", *HARNESS_GIT_CONFIG, *args]


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


def _refuse_root(root: Path, home: Path) -> None:
    """A writable root must not be ``$HOME``, a directory above it, or one above ``~/.ssh`` or the k3code home."""
    for guarded in (home, home / ".ssh", paths.home()):
        target = guarded.resolve()
        if root == target or root in target.parents:
            raise SandboxRefused(
                f"refusing to make {root} writable inside the sandbox: it is or contains $HOME, ~/.ssh or the "
                "k3code home; start the session in a project folder"
            )


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


def exposes_home(path: Path | str, home: Path | None = None) -> bool:
    """True when binding ``path`` would bring back all of ``$HOME``: it is ``$HOME`` or contains it (``/``)."""
    home = Path(home or Path.home()).resolve()
    return home.is_relative_to(Path(path).resolve())


def build_argv(
    cwd: Path | str,
    add_dirs: Iterable[Path | str] = (),
    *,
    home: Path | None = None,
    bwrap: str | None = None,
    network: bool = True,
) -> list[str]:
    """The ``bwrap`` argv *prefix*; append the command (e.g. ``/bin/sh -c "..."``).

    A project dir that is ``$HOME`` or contains it (``/``) is not bound: that would undo the home tmpfs and hand the
    command ``~/.config/k3code/env`` and ``~/.ssh``. Those two are masked again after the binds in any case.

    Raises :class:`SandboxRefused` when the cwd or an added dir is ``$HOME``, a directory above it, or a directory
    above ``~/.ssh`` or the k3code home.
    """
    home = Path(home or Path.home())
    roots = [Path(cwd).resolve(), *(Path(d).resolve() for d in add_dirs)]
    for root in roots:
        _refuse_root(root, home)
    argv = [bwrap or bwrap_path() or "bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc"]
    if not network:
        argv.append("--unshare-net")
    # The command gets a minimal environment, not the daemon's: it carries the provider API keys (OMNIROUTE_API_KEY,
    # ANTHROPIC_API_KEY, ...), so any command run by a prompt-injected turn could print them.
    argv.append("--clearenv")
    for name in ENV_ALLOW:
        value = os.environ.get(name)
        if value is not None:
            argv += ["--setenv", name, value]
    # System: read-only. /bin, /lib* are usually symlinks into /usr; recreate them as symlinks.
    for path in ("/usr", "/etc"):
        if os.path.isdir(path):
            argv += ["--ro-bind", path, path]
    for path in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):
        if os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            argv += ["--ro-bind", path, path]
    # /etc/resolv.conf is often a link into /run; keep name resolution working (when the network is on).
    for path in ("/run/systemd/resolve", "/run/NetworkManager"):
        if os.path.isdir(path):
            argv += ["--ro-bind", path, path]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    # $HOME hidden behind an empty tmpfs, then only the cache dirs and the project are bound back.
    argv += ["--tmpfs", str(home)]
    cache = home / HOME_CACHE
    if cache.is_dir():
        argv += ["--bind", str(cache), str(cache)]
    uv = home / HOME_UV
    if uv.is_dir():
        argv += ["--ro-bind", str(uv), str(uv)]
    seen: set[str] = set()
    for root in roots:
        p = str(root)
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            if exposes_home(p, home):
                logger.warning("sandbox: not binding %s (it would expose $HOME)", p)
                continue
            argv += ["--bind", p, p]
            # git metadata is read-only: a sandboxed command cannot write a hook or change core.* config
            for meta in _git_metadata(root):
                argv += ["--ro-bind", str(meta), str(meta)]
    # $HOME secrets are masked again after the project binds: a bind above them would re-expose them
    xdg = os.environ.get("XDG_CONFIG_HOME")
    secrets = [home / s for s in HOME_SECRETS] + ([Path(xdg) / "k3code"] if xdg else [])
    for path in dict.fromkeys(str(p) for p in secrets):
        if os.path.isdir(path):
            argv += ["--tmpfs", path]
    argv += ["--chdir", str(roots[0])]
    return argv


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
