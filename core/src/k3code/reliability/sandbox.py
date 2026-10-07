"""bubblewrap sandbox for unattended bash: read-only system, writable project, hidden ``$HOME``.

Used when a session is in ``auto``/``yolo`` mode or is a background/cron/loop session. The network
stays on (providers and tools need it). Without ``bwrap`` (or where user namespaces are disabled)
callers fall back to running unsandboxed and ``/doctor`` warns.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from k3code.permissions import PermissionMode

#: ``$HOME`` entries that stay visible inside the sandbox (rw cache, ro uv-managed pythons).
HOME_CACHE = ".cache"
HOME_UV = ".local/share/uv"
SANDBOXED_MODES = (PermissionMode.AUTO, PermissionMode.YOLO)


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def should_sandbox(mode: PermissionMode | str, background: bool) -> bool:
    """auto/yolo permission modes and background/cron/loop sessions run sandboxed."""
    return background or PermissionMode(mode) in SANDBOXED_MODES


def build_argv(
    cwd: Path | str,
    add_dirs: Iterable[Path | str] = (),
    *,
    home: Path | None = None,
    bwrap: str | None = None,
) -> list[str]:
    """The ``bwrap`` argv *prefix*; append the command (e.g. ``/bin/sh -c "..."``)."""
    home = Path(home or Path.home())
    argv = [bwrap or bwrap_path() or "bwrap", "--die-with-parent", "--new-session", "--unshare-pid"]
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
            argv += ["--bind", p, p]
    argv += ["--chdir", str(Path(cwd).resolve())]
    return argv


@functools.cache
def usable() -> bool:
    """True when bwrap exists and can really create the sandbox here (user namespaces enabled)."""
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
