"""Call-time path helpers (``K3CODE_HOME`` is read on every call, never cached)."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("K3CODE_HOME", str(Path.home() / ".k3code"))).expanduser()


def user_config_path() -> Path:
    return home() / "config.yaml"


def project_config_path(cwd: str | Path) -> Path:
    return Path(cwd) / ".k3code" / "config.yaml"


def data_dir() -> Path:
    """Install root (``versions/``, ``current``, ``node/``): ``K3CODE_DATA`` or ``~/.local/share/k3code``."""
    return Path(os.environ.get("K3CODE_DATA", str(Path.home() / ".local" / "share" / "k3code"))).expanduser()


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
