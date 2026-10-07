"""Auto-detect the host environment for the *System and environment* step."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

TOOLCHAINS = {
    "python": "python3",
    "node": "node",
    "go": "go",
    "rust": "cargo",
    "docker": "docker",
}


def _git(key: str) -> str:
    if not shutil.which("git"):
        return ""
    r = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True, check=False)
    return r.stdout.strip()


def detect() -> dict[str, Any]:
    return {
        "os": f"{platform.system()} {platform.machine()}",
        "shell": Path(os.environ.get("SHELL", "")).name,
        "terminal": os.environ.get("TERM_PROGRAM") or os.environ.get("TERM", ""),
        "editor": os.environ.get("VISUAL") or os.environ.get("EDITOR", ""),
        "git_name": _git("user.name"),
        "git_email": _git("user.email"),
        "toolchains": sorted(name for name, exe in TOOLCHAINS.items() if shutil.which(exe)),
        "tailscale": shutil.which("tailscale") is not None,
    }
