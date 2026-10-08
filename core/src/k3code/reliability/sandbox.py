"""bubblewrap sandbox for unattended bash: read-only system, writable project, hidden ``$HOME``.

Used when a session is in ``auto``/``yolo`` mode or is a background/cron/loop session. The network
stays on (providers and tools need it). Without ``bwrap`` (or where user namespaces are disabled)
callers fall back to running unsandboxed and ``/doctor`` warns.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

from k3code.permissions import PermissionMode

logger = logging.getLogger(__name__)

#: ``$HOME`` entries that stay visible inside the sandbox (rw cache, ro uv-managed pythons).
HOME_CACHE = ".cache"
HOME_UV = ".local/share/uv"
SANDBOXED_MODES = (PermissionMode.AUTO, PermissionMode.YOLO)
#: Environment variables the sandboxed command inherits (everything else, in particular API keys, is dropped).
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "NO_COLOR", "COLORTERM")
#: A failed probe is retried after this many seconds (one slow probe used to disable the sandbox for the whole
#: daemon lifetime: every unattended session then ran bash on the real $HOME).
REPROBE_AFTER_S = 60.0
#: ``$HOME`` entries masked again after the project binds (a project bind can re-expose them: credentials, keys).
HOME_SECRETS = (".config/k3code", ".ssh")


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def should_sandbox(mode: PermissionMode | str, background: bool) -> bool:
    """auto/yolo permission modes and background/cron/loop sessions run sandboxed."""
    return background or PermissionMode(mode) in SANDBOXED_MODES


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
) -> list[str]:
    """The ``bwrap`` argv *prefix*; append the command (e.g. ``/bin/sh -c "..."``).

    A project dir that is ``$HOME`` or contains it (``/``) is not bound: that would undo the home tmpfs and hand the
    command ``~/.config/k3code/env`` and ``~/.ssh``. Those two are masked again after the binds in any case."""
    home = Path(home or Path.home())
    argv = [bwrap or bwrap_path() or "bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc"]
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
    # /etc/resolv.conf is often a link into /run; keep name resolution working (network stays on).
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
    for d in (cwd, *add_dirs):
        p = str(Path(d).resolve())
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            if exposes_home(p, home):
                logger.warning("sandbox: not binding %s (it would expose $HOME)", p)
                continue
            argv += ["--bind", p, p]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    secrets = [home / s for s in HOME_SECRETS] + ([Path(xdg) / "k3code"] if xdg else [])
    for path in dict.fromkeys(str(p) for p in secrets):
        if os.path.isdir(path):
            argv += ["--tmpfs", path]
    argv += ["--chdir", str(Path(cwd).resolve())]
    return argv


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
        r = subprocess.run(
            [*build_argv(tempfile.gettempdir(), bwrap=path), "/bin/true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
