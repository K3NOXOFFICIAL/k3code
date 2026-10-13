"""bubblewrap sandbox for auto/yolo bash and unattended children: read-only system, writable project, hidden ``$HOME``.

Sandboxed: auto/yolo sessions, and every unattended session (background run, loop tick, cron job, goal continuation,
sub-agent) whatever its mode. Inside the sandbox ``$HOME`` is an empty tmpfs (only the cache dirs come back), the
project is writable, and its git metadata is read-only so a command cannot plant a hook. The network stays on for
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
import errno
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
from k3code.paths import GATEWAY_ENV_VARS
from k3code.permissions import PermissionMode
from k3code.permissions.engine import windows_drives

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
#: change those files (see :func:`k3code.permissions.engine.protected_path`) and the sandbox binds the git metadata
#: read-only. It stays open for a repository config changed by anything else (an unsandboxed command someone
#: approved, a process outside k3code) and for a driver the user keeps in the repository on purpose.
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
) -> list[str]:
    """The ``bwrap`` argv *prefix*; append the command (e.g. ``/bin/sh -c "..."``).

    ``home_readonly`` lists ``$HOME`` entries that stay visible read-only (default: ``sandbox.home_readonly`` of the
    user's config). It exists for small marker files a tool must be able to read, e.g. ``.myapp``.

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
