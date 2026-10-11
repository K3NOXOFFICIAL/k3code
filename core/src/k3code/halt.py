"""Global halt (``/daemon pause``): one persisted flag that stops every model turn, loop tick, job and sub-agent.

The flag lives in ``$K3CODE_HOME/halt.json`` so it survives a daemon restart. Only ``/daemon resume`` clears it:
neither the restart-storm guard, the safe-mode auto-clear nor the watchdog may override it. A flag file that is damaged
(not JSON, not an object, unreadable) counts as a halt: a safety flag must not turn itself off by breaking.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Halt:
    reason: str
    since: float


def halt_path(home: Path) -> Path:
    return home / "halt.json"


def load_halt(home: Path) -> Halt | None:
    """The persisted halt, or None when the daemon is not halted.

    No file (or an empty one, which this module never writes) is not halted. A file that is there but cannot be read
    or understood fails CLOSED: it is a halt (its reason says so), because a halt flag that stops holding when it is
    damaged is no safety. ``/daemon resume`` deletes the file whatever it holds, so this cannot lock anyone out."""
    path = halt_path(home)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _damaged(path, f"cannot be read: {e}")
    if not text.strip():
        logger.warning("%s is empty; treating the daemon as not halted", path)
        return None
    try:
        data = json.loads(text)
    except ValueError as e:
        return _damaged(path, f"is not valid JSON: {e}")
    if not isinstance(data, dict):
        return _damaged(path, "does not hold an object")
    if not data.get("active", True):
        return None
    try:
        since = float(data.get("since") or 0.0)
    except (TypeError, ValueError):
        since = 0.0  # still halted: a bad timestamp is only a bad timestamp
    return Halt(reason=str(data.get("reason") or "halted"), since=since)


def _damaged(path: Path, why: str) -> Halt:
    logger.error("%s %s; the daemon stays halted until /daemon resume", path, why)
    return Halt(reason=f"{path.name} {why} (damaged halt flag; /daemon resume clears it)", since=0.0)


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
