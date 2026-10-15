"""Resumable setup state (``$K3CODE_HOME/setup_state.json``) and the 0600 env file for secrets."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code.paths import ensure_private_dir, home, private_file


def state_path() -> Path:
    return home() / "setup_state.json"


def env_file_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "k3code" / "env"


def load_state(say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The saved progress, or a fresh state. A file that is not usable state (cut off, not an object, wrong-typed
    ``completed``/``data``) is moved aside to ``setup_state.json.bad`` and reported through ``say``."""
    path = state_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"completed": [], "data": {}}
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        data.setdefault("completed", [])
        data.setdefault("data", {})
        if (
            isinstance(data["completed"], list)
            and all(isinstance(n, str) for n in data["completed"])
            and isinstance(data["data"], dict)
        ):
            return data
    backup = path.with_name(path.name + ".bad")
    with contextlib.suppress(OSError):
        os.replace(path, backup)  # a rename keeps the file's 0600 mode
    if say:
        say(f"Setup: saved progress was unreadable, starting over (the old file is kept at {backup}).")
    return {"completed": [], "data": {}}


def save_state(state: dict[str, Any]) -> None:
    p = state_path()
    ensure_private_dir(p.parent)  # $K3CODE_HOME: 0700 whatever the umask (issue #163)
    tmp = p.with_suffix(".tmp")
    private_file(tmp).write_text(json.dumps(state, indent=2, sort_keys=True))  # setup answers: 0600
    os.replace(tmp, p)


def clear_state() -> None:
    state_path().unlink(missing_ok=True)


def _env_key(line: str) -> str:
    """The variable name of an env-file line (an optional leading ``export`` is not part of it)."""
    k = line.split("=", 1)[0].strip()
    return k[len("export ") :].strip() if k.startswith("export ") else k


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """``NAME=value`` lines; also ``export NAME=value`` and quoted values (the shell form people paste in)."""
    path = path or env_file_path()
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                out[_env_key(line)] = v
    return out


def _write_env_lines(path: Path, lines: list[str]) -> None:
    """Replace the env file with ``lines`` atomically; the file is always mode 0600."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n" if lines else "")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def set_env_var(name: str, value: str, path: Path | None = None) -> Path:
    """Add/replace ``name=value`` in the env file, keeping other lines; file is always mode 0600."""
    path = path or env_file_path()
    ensure_private_dir(path.parent)  # ~/.config/k3code holds provider keys: 0700 whatever the umask (issue #163)
    lines = path.read_text().splitlines() if path.is_file() else []
    new = [ln for ln in lines if _env_key(ln) != name]
    new.append(f"{name}={value}")
    _write_env_lines(path, new)
    return path


def unset_env_var(name: str, path: Path | None = None) -> bool:
    """Remove every ``name=...`` line from the env file, keeping the others (mode 0600). False when it had none."""
    path = path or env_file_path()
    if not path.is_file():
        return False
    lines = path.read_text().splitlines()
    new = [ln for ln in lines if _env_key(ln) != name]
    if len(new) == len(lines):
        return False
    _write_env_lines(path, new)
    return True
