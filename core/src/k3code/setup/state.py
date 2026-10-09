"""Resumable setup state (``$K3CODE_HOME/setup_state.json``) and the 0600 env file for secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from k3code.paths import home, private_file


def state_path() -> Path:
    return home() / "setup_state.json"


def env_file_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "k3code" / "env"


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("completed", [])
    data.setdefault("data", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
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


def set_env_var(name: str, value: str, path: Path | None = None) -> Path:
    """Add/replace ``name=value`` in the env file, keeping other lines; file is always mode 0600."""
    path = path or env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.is_file() else []
    new = [ln for ln in lines if _env_key(ln) != name]
    new.append(f"{name}={value}")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(new) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
