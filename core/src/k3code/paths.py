"""Call-time path helpers (``K3CODE_HOME`` is read on every call, never cached)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Environment variables that point a process at the daemon's socket. Never passed to a child process: only an
#: authenticated client (k3code.gateway.auth) may drive the daemon.
GATEWAY_ENV_VARS = ("K3CODE_GATEWAY_SOCKET", "HERMES_TUI_GATEWAY_URL")


def home() -> Path:
    return Path(os.environ.get("K3CODE_HOME", str(Path.home() / ".k3code"))).expanduser()


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` as 0700, or tighten an existing one we own to 0700. Refuses a directory another user owns.

    k3code's private state (sessions, journals, the gateway token, learned notes) gets its mode here and in
    :func:`private_file`, never from the umask: the service unit runs with the user's umask (0022)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.stat()
    if st.st_uid != os.getuid():
        raise RuntimeError(f"{path} is owned by another user; k3code keeps private state there")
    if st.st_mode & 0o777 != 0o700:
        path.chmod(0o700)
    return path


def private_file(path: Path) -> Path:
    """Create ``path`` empty when it is missing and make it 0600 whatever the umask (an existing file we own is
    tightened). A missing parent is created 0700. sqlite gives a database's -journal/-wal/-shm files its mode, so
    calling this before ``sqlite3.connect`` covers them too."""
    import contextlib
    import stat

    if not path.parent.is_dir():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            with contextlib.suppress(PermissionError):  # another user's file: leave it, as before
                os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return path


def project_key(root: str | Path) -> str:
    """The ``$K3CODE_HOME/projects/<key>`` name of a project: its path with separators flattened.

    The same key as the TUI's ``projectHistoryKey`` (tui/src/lib/history.ts), so both sides share one directory.
    """
    import re

    return re.sub(r"[/:\\]+", "_", str(root)) or "default"


def project_state_dir(root: str | Path) -> Path:
    """Per-project state k3code keeps outside the repository (input history, learned notes)."""
    return home() / "projects" / project_key(root)


def user_config_path() -> Path:
    return home() / "config.yaml"


def project_config_path(cwd: str | Path) -> Path:
    return Path(cwd) / ".k3code" / "config.yaml"


def data_dir() -> Path:
    """Install root (``versions/``, ``current``, ``node/``): ``K3CODE_DATA`` or ``~/.local/share/k3code``."""
    return Path(os.environ.get("K3CODE_DATA", str(Path.home() / ".local" / "share" / "k3code"))).expanduser()


def install_roots(module_file: Path | None = None) -> list[Path]:
    """Directories k3code may run its own code from (tui/dist/entry.js, scripts/vendor_check.py), best first.

    Never the cwd or an arbitrary parent of the package: a repo you clone (or a venv inside it) could ship those
    files. ``K3CODE_ROOT`` names a dev checkout explicitly; otherwise the source tree this package was imported
    from (core/src/k3code), the installed version dir (``<data>/versions/<ver>``) and ``<data>/current``.
    """
    here = (module_file or Path(__file__)).resolve()
    roots: list[Path] = []
    if explicit := os.environ.get("K3CODE_ROOT"):
        roots.append(Path(explicit).expanduser())
    if len(here.parents) > 3 and here.parents[0].name == "k3code" and here.parents[1].name == "src":
        source = here.parents[3]
        if here.parents[2] == source / "core" and (source / "core" / "pyproject.toml").is_file():
            roots.append(source)
    versions = (data_dir() / "versions").resolve()
    roots.extend(p for p in here.parents if p.parent == versions)
    roots.append(data_dir() / "current")
    return roots


#: The oldest Node major the TUI runs on (install.sh asks for the same).
MIN_NODE_MAJOR = 20
#: Environment variable that names the node to use, whatever its version.
NODE_ENV = "K3CODE_NODE"


@dataclass(frozen=True)
class NodeChoice:
    """The node ``find_node`` settled on, or why there is none."""

    path: str | None
    version: str = ""  # "v22.23.3"
    source: str = ""  # "K3CODE_NODE", "private" or "PATH"
    reason: str = ""  # why this node was chosen, or what is wrong when ``path`` is None
    old: str = ""  # a node that was found but is older than MIN_NODE_MAJOR, "v18.19.1 at /usr/bin/node"

    @property
    def major(self) -> int:
        return _parse_node_version(self.version)[0]


def _parse_node_version(text: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def _node_version(path: str) -> str:
    """``node --version`` of ``path`` ("v22.23.3"), or "" when it does not run or prints no version."""
    import subprocess

    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.match(r"\s*v(\d+\.\d+\.\d+)", out)
    return f"v{m.group(1)}" if m else ""


def resolve_node() -> NodeChoice:
    """Pick the node that runs the TUI: ``K3CODE_NODE``, else the installer's private Node, else one on PATH.

    The private Node (``<data>/node/<ver>/bin/node``, newest first) and the PATH node must both be at least
    ``MIN_NODE_MAJOR``: an old distro node first on PATH used to win and crash the TUI at start. ``K3CODE_NODE`` is
    taken as given (an explicit choice), but a value that is not a working node is an error, never a silent fallback.
    """
    import shutil

    fix = f"set {NODE_ENV} to a node binary, or run the k3code installer (it fetches a private Node)"
    explicit = os.environ.get(NODE_ENV, "").strip()
    if explicit:
        found = shutil.which(os.path.expanduser(explicit))
        version = _node_version(found) if found else ""
        if not found or not version:
            return NodeChoice(None, reason=f"{NODE_ENV}={explicit} is not a working node; {fix}")
        return NodeChoice(found, version, NODE_ENV, f"set by {NODE_ENV}")
    privates = sorted(
        (p for p in (data_dir() / "node").glob("*/bin/node") if p.is_file()),
        key=lambda p: _parse_node_version(p.parent.parent.name),
        reverse=True,
    )
    candidates = [("private", str(p), "the private Node from the installer") for p in privates]
    if on_path := shutil.which("node"):
        candidates.append(("PATH", on_path, "the node on PATH"))
    old = ""
    for source, path, what in candidates:
        version = _node_version(path)
        if not version:
            continue
        if _parse_node_version(version)[0] < MIN_NODE_MAJOR:
            old = old or f"{version} at {path}"
            continue
        return NodeChoice(path, version, source, f"{what}, Node >= {MIN_NODE_MAJOR}")
    if old:
        return NodeChoice(None, reason=f"Node >= {MIN_NODE_MAJOR} required (found {old}); {fix}", old=old)
    return NodeChoice(None, reason=f"Node >= {MIN_NODE_MAJOR} not found; {fix}")


def find_node() -> str | None:
    """The node to run the TUI with (see ``resolve_node``), or None."""
    return resolve_node().path


def node_problem() -> str:
    """Why ``find_node`` found nothing, as one sentence for the user."""
    return resolve_node().reason
