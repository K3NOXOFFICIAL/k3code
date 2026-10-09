"""Per-project state k3code keeps outside the repository: ``$K3CODE_HOME/projects/<project>/``.

``project.json`` (detected stacks, commands, fingerprint, what was proposed and accepted), ``rules.yaml`` (allow
rules accepted from project recipes, applied in this project only) and ``mcp.json`` (MCP servers accepted from
project recipes, started in this project only). The directory is the one memory and ``.mcp.json`` choices use
(:func:`k3code.paths.project_state_dir` of the repository root).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_FILE = "project.json"
RULES_FILE = "rules.yaml"
MCP_FILE = "mcp.json"
VERSION = 2


def project_root(cwd: str | Path) -> Path:
    from k3code.memory import project_root as root_of

    return root_of(cwd)


def state_dir(cwd: str | Path) -> Path:
    from k3code.paths import project_state_dir

    return project_state_dir(project_root(cwd))


def state_path(cwd: str | Path) -> Path:
    return state_dir(cwd) / STATE_FILE


def rules_path(cwd: str | Path) -> Path:
    return state_dir(cwd) / RULES_FILE


def mcp_path(cwd: str | Path) -> Path:
    return state_dir(cwd) / MCP_FILE


def legacy_path(root: str | Path) -> Path:
    """Where older versions wrote project.json: inside the repository. Read once for migration, never written."""
    return Path(root) / ".k3code" / "project.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(cwd: str | Path) -> dict[str, Any]:
    return _read_json(state_path(cwd))


def save(cwd: str | Path, state: dict[str, Any]) -> Path:
    path = state_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def update(cwd: str | Path, fn: Any) -> dict[str, Any]:
    """Load, let ``fn(state)`` change it in place, save; returns the state."""
    state = load(cwd)
    fn(state)
    save(cwd, state)
    return state


def accepted(state: dict[str, Any]) -> dict[str, Any]:
    acc = state.setdefault("accepted", {})
    acc.setdefault("skills", [])
    acc.setdefault("commands", {})
    return acc


def mcp_servers(cwd: str | Path) -> dict[str, Any]:
    """The project's recipe MCP servers as ``McpServerConfig``s (k3code's own format, written on acceptance)."""
    from k3code.config import McpServerConfig

    out: dict[str, Any] = {}
    servers = _read_json(mcp_path(cwd)).get("servers")
    for name, spec in servers.items() if isinstance(servers, dict) else []:
        if isinstance(spec, dict):
            try:
                out[str(name)] = McpServerConfig(**spec)
            except (TypeError, ValueError):
                continue
    return out


def add_mcp_server(cwd: str | Path, name: str, spec: dict[str, Any]) -> Path:
    path = mcp_path(cwd)
    data = _read_json(path)
    servers = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    servers[name] = spec
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"servers": servers}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path
