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


def sandbox_state(probe: bool = True) -> str:
    """``usable``, ``unusable`` (bwrap present but blocked), ``bubblewrap`` (bwrap is on PATH, not probed),
    ``missing``, or ``unknown`` (the probe failed). Never raises: the setup wizard must not fail on a sandbox probe."""
    try:
        from k3code import doctor
        from k3code.reliability import sandbox

        if sandbox.bwrap_path() is None:
            return "missing"
        if not probe:  # the same detection as `k3code doctor --no-probe`
            return "bubblewrap" if doctor.check_sandbox(probe=False).status == doctor.OK else "unknown"
        return "usable" if sandbox.usable() else "unusable"
    except Exception:  # noqa: BLE001 - reported as unknown, the wizard goes on
        return "unknown"


def detect(probe: bool = True) -> dict[str, Any]:
    return {
        "sandbox": sandbox_state(probe),
        "os": f"{platform.system()} {platform.machine()}",
        "shell": Path(os.environ.get("SHELL", "")).name,
        "terminal": os.environ.get("TERM_PROGRAM") or os.environ.get("TERM", ""),
        "editor": os.environ.get("VISUAL") or os.environ.get("EDITOR", ""),
        "git_name": _git("user.name"),
        "git_email": _git("user.email"),
        "toolchains": sorted(name for name, exe in TOOLCHAINS.items() if shutil.which(exe)),
        "tailscale": shutil.which("tailscale") is not None,
    }
