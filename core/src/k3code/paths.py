"""Call-time path helpers (``K3CODE_HOME`` is read on every call, never cached)."""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variables that point a process at the daemon's socket. Never passed to a child process: only an
#: authenticated client (k3code.gateway.auth) may drive the daemon.
GATEWAY_ENV_VARS = ("K3CODE_GATEWAY_SOCKET", "HERMES_TUI_GATEWAY_URL")


def home() -> Path:
    return Path(os.environ.get("K3CODE_HOME", str(Path.home() / ".k3code"))).expanduser()


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


def find_node() -> str | None:
    """``node`` on PATH, else the one the installer put in ``<data>/node/<ver>/bin``."""
    import shutil

    found = shutil.which("node")
    if found:
        return found
    for cand in sorted((data_dir() / "node").glob("*/bin/node"), reverse=True):
        if cand.is_file():
            return str(cand)
    return None
