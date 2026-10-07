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
