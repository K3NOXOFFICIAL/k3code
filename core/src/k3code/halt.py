"""Global halt (``/daemon pause``): one persisted flag that stops every model turn, loop tick, job and sub-agent.

The flag lives in ``$K3CODE_HOME/halt.json`` so it survives a daemon restart. Only ``/daemon resume`` clears it:
neither the restart-storm guard, the safe-mode auto-clear nor the watchdog may override it.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Halt:
    reason: str
    since: float


def halt_path(home: Path) -> Path:
    return home / "halt.json"


def load_halt(home: Path) -> Halt | None:
    """The persisted halt, or None when the daemon is not halted (or the file is unreadable)."""
    try:
        data = json.loads(halt_path(home).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("active", True):
        return None
    return Halt(reason=str(data.get("reason") or "halted"), since=float(data.get("since") or 0.0))


def set_halt(home: Path, reason: str, now: float | None = None) -> Halt:
    """Persist the halt (atomic write, so a crash mid-write never leaves a half-written flag)."""
    halt = Halt(reason=reason, since=time.time() if now is None else now)
    home.mkdir(parents=True, exist_ok=True)
    tmp = halt_path(home).with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"active": True, **asdict(halt)}))
    os.replace(tmp, halt_path(home))
    return halt


def clear_halt(home: Path) -> bool:
    """Remove the persisted halt; returns whether there was one."""
    existed = halt_path(home).exists()
    with contextlib.suppress(OSError):
        halt_path(home).unlink()
    return existed
