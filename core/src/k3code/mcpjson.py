"""A project's ``.mcp.json`` (Claude Code's format), read once the project is trusted, started per server on request.

``{"mcpServers": {"name": {"command": ..., "args": [...], "env": {...}} | {"type": "http", "url": ..., "headers":
{...}}}}``. Trusting the project lets k3code read the file and list its servers in ``/mcp`` as available; none of
them starts until the user runs ``/mcp enable <name>``. That choice is kept outside the repository, in
``$K3CODE_HOME/projects/<project>/mcp_enabled.json``, with a digest of the server's definition: a changed definition
is not started until it is enabled again. Servers in ``mcp.servers`` of a config file win on a name clash. ``env``
and ``headers`` values are passed as written (no ``${VAR}`` expansion).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from k3code.config import McpServerConfig
from k3code.paths import project_state_dir

logger = logging.getLogger(__name__)

FILE = ".mcp.json"
STATE_FILE = "mcp_enabled.json"


def _str_map(value: object) -> dict[str, str]:
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def parse(text: str) -> dict[str, McpServerConfig]:
    """The servers the file declares that k3code can run (stdio, streamable HTTP); others are skipped."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    out: dict[str, McpServerConfig] = {}
    for name, spec in servers.items() if isinstance(servers, dict) else []:
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type") or ("http" if spec.get("url") else "stdio"))
        if kind == "http" and spec.get("url"):
            out[str(name)] = McpServerConfig(url=str(spec["url"]), headers=_str_map(spec.get("headers")))
        elif kind == "stdio" and spec.get("command"):
            args = [str(a) for a in spec.get("args") or []] if isinstance(spec.get("args"), list) else []
            out[str(name)] = McpServerConfig(command=str(spec["command"]), args=args, env=_str_map(spec.get("env")))
        else:
            logger.info("%s: server %s (type %s) is not supported, skipped", FILE, name, kind)
    return out


def declared(project_dir: str | Path) -> dict[str, McpServerConfig]:
    """The project's ``.mcp.json`` servers; empty unless the user trusted the project (k3code.trust)."""
    from k3code import trust

    path = Path(project_dir) / FILE
    if not path.is_file() or trust.decision(project_dir) != trust.TRUSTED:
        return {}
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def digest(cfg: McpServerConfig) -> str:
    return hashlib.sha256(json.dumps(cfg.model_dump(), sort_keys=True).encode("utf-8")).hexdigest()


def _state_path(project_dir: str | Path) -> Path:
    return project_state_dir(Path(project_dir).resolve()) / STATE_FILE


def _load_state(project_dir: str | Path) -> dict[str, str]:
    try:
        data = json.loads(_state_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _str_map(data.get("enabled")) if isinstance(data, dict) else {}


def set_enabled(project_dir: str | Path, name: str, cfg: McpServerConfig | None) -> None:
    """Enable ``name`` as defined by ``cfg``, or (``cfg`` None) disable it."""
    state = _load_state(project_dir)
    if cfg is None:
        state.pop(name, None)
    else:
        state[name] = digest(cfg)
    path = _state_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"enabled": state}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def split(project_dir: str | Path) -> tuple[dict[str, McpServerConfig], list[str]]:
    """(enabled servers whose definition is unchanged, names that are only available)."""
    servers = declared(project_dir)
    state = _load_state(project_dir)
    enabled = {n: c for n, c in servers.items() if state.get(n) == digest(c)}
    return enabled, sorted(n for n in servers if n not in enabled)


def merged(config_servers: dict[str, McpServerConfig], project_dir: str | Path) -> dict[str, McpServerConfig]:
    """The servers to run: the project's enabled ``.mcp.json`` servers, the ones the user accepted from a project
    recipe for this project (k3code.learning.recipes), then the config's (the config wins)."""
    from k3code.learning.projectstate import mcp_servers

    enabled, _ = split(project_dir)
    return {**enabled, **mcp_servers(project_dir), **config_servers}
