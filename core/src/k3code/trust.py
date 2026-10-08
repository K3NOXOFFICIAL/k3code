"""Trust for a project's ``.k3code/config.yaml``.

A repo you clone can ship a config that starts MCP servers (commands), adds permission rules that approve tool
calls without asking, or points the provider chain somewhere else. That config is applied only after you agree to
this exact file. The answer is stored in ``$K3CODE_HOME/trusted_projects.json`` per absolute project path, with the
SHA-256 of the file, so a changed file asks again.

Interactive opens (the TUI and the REPL) ask once, in the parent process, before anything is loaded. Headless and
piped runs never ask: an untrusted project config is ignored and ``main()`` says so in one line.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from k3code.paths import home

STORE_NAME = "trusted_projects.json"
#: decision() values
NONE = "none"  # no project config, or nothing in it
TRUSTED = "trusted"
DECLINED = "declined"
UNDECIDED = "undecided"  # never answered, or the file changed since the answer

_MAX_RULES_SHOWN = 8
_MAX_TEXT = 140


def config_path(project_dir: str | Path) -> Path:
    return Path(project_dir).expanduser() / ".k3code" / "config.yaml"


def _is_user_config(path: Path) -> bool:
    """The user's own ``$K3CODE_HOME/config.yaml`` is never a project config (a session started in $HOME sees both)."""
    try:
        return path.resolve() == (home() / "config.yaml").resolve()
    except OSError:
        return False


def project_dir_of(path: str | Path) -> Path | None:
    """The project directory whose config ``path`` is, or None when ``path`` is not a project config."""
    p = Path(path)
    if p.name != "config.yaml" or p.parent.name != ".k3code" or _is_user_config(p):
        return None
    return p.parent.parent


def _key(project_dir: str | Path) -> str:
    return str(Path(project_dir).expanduser().resolve())


def _read(project_dir: str | Path) -> bytes | None:
    path = config_path(project_dir)
    if _is_user_config(path):
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _store() -> dict[str, Any]:
    try:
        data = json.loads((home() / STORE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # unreadable store: every project config is untrusted until answered again
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}


def _save(projects: dict[str, Any]) -> None:
    path = home() / STORE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "projects": projects}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _record_for(project_dir: str | Path) -> dict[str, Any] | None:
    rec = _store().get(_key(project_dir))
    return rec if isinstance(rec, dict) else None


def decision(project_dir: str | Path) -> str:
    """NONE, TRUSTED, DECLINED or UNDECIDED for the project's config as it is on disk now."""
    raw = _read(project_dir)
    if raw is None or not raw.strip():
        return NONE
    rec = _record_for(project_dir)
    if rec is not None and rec.get("sha256") == _digest(raw):
        return TRUSTED if rec.get("trusted") is True else DECLINED
    return UNDECIDED


def trusted_text(project_dir: str | Path) -> str | None:
    """The config text when the user trusted exactly these bytes; None when the config must be ignored."""
    raw = _read(project_dir)
    if raw is None:
        return None
    rec = _record_for(project_dir)
    if rec is None or rec.get("trusted") is not True or rec.get("sha256") != _digest(raw):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def record(project_dir: str | Path, *, trusted: bool) -> bool:
    """Remember the answer for the file as it is now. False when the project has no config file."""
    raw = _read(project_dir)
    if raw is None:
        return False
    projects = _store()
    projects[_key(project_dir)] = {
        "sha256": _digest(raw),
        "trusted": trusted,
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _save(projects)
    return True


def revoke(project_dir: str | Path) -> bool:
    """Forget the answer: the config is ignored again until it is trusted. False when there was none."""
    projects = _store()
    if projects.pop(_key(project_dir), None) is None:
        return False
    _save(projects)
    return True


@contextmanager
def keeping_trust(path: str | Path) -> Iterator[None]:
    """Around k3code's own write to a project config: the result is trusted when the file was trusted or absent.

    Only a write that k3code makes itself (an approved rule, a /config edit) goes through here, and the user asked
    for it; an edit made by the agent with its file tools does not, so it has to be trusted again. A file that
    was missing or empty holds nothing anyone else wrote, so what k3code creates in it is trusted at once.
    An existing file that was never trusted stays untrusted.
    """
    project = project_dir_of(path)
    was_trusted = project is not None and decision(project) in (TRUSTED, NONE)
    yield
    if was_trusted and project is not None:
        record(project, trusted=True)


def problem(project_dir: str | Path) -> str | None:
    """Why the file can never be applied (it does not parse, or its top level is not a mapping); None when it can."""
    raw = _read(project_dir)
    if raw is None or not raw.strip():
        return None
    try:
        data = yaml.safe_load(raw.decode("utf-8", errors="replace"))
    except yaml.YAMLError:
        return "it does not parse as YAML"
    return None if data is None or isinstance(data, dict) else "its top level is not a mapping"


def summary(project_dir: str | Path) -> list[str] | None:
    """What the project config changes, one short line each; None when there is no config or it has no settings."""
    raw = _read(project_dir)
    if raw is None or not raw.strip():
        return None
    return describe(raw.decode("utf-8", errors="replace"))


def describe(text: str) -> list[str]:
    """Lines naming MCP servers, permission rules and providers, and the other top-level keys by name.

    Values that can hold secrets (MCP ``env`` and ``headers``, provider keys) are never shown.
    """
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return ["the file does not parse as YAML, so it is ignored"]
    if not isinstance(data, dict):
        return ["the file is not a mapping, so it is ignored"]
    lines: list[str] = []

    mcp = data.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    for name, spec in servers.items() if isinstance(servers, dict) else []:
        spec = spec if isinstance(spec, dict) else {}
        args = spec.get("args") if isinstance(spec.get("args"), list) else []
        if spec.get("command"):
            command = " ".join(str(part) for part in [spec["command"], *args])
            lines.append(f"MCP server {name} runs: {_short(command)}")
        elif spec.get("url"):
            lines.append(f"MCP server {name} connects to: {_short(str(spec['url']))}")

    perms = data.get("permissions")
    if isinstance(perms, dict):
        rules = _permission_rules(perms)
        auto = sum(1 for action, _, _ in rules if action == "allow")
        if rules:
            lines.append(f"permission rules: {len(rules)} ({auto} allow without asking)")
        for action, tool, pattern in rules[:_MAX_RULES_SHOWN]:
            shown = f"{action} {tool}" + ("" if pattern == "*" else f" `{_short(pattern, 80)}`")
            lines.append(f"  {shown}")
        if len(rules) > _MAX_RULES_SHOWN:
            lines.append(f"  ... and {len(rules) - _MAX_RULES_SHOWN} more")

    providers = data.get("providers")
    if isinstance(providers, list):
        entries = [p for p in providers if isinstance(p, dict)]
        if entries:
            parts = [
                f"{p.get('name', '?')} ({p.get('kind', '?')}) at {_short(str(p.get('base_url', '-')), 80)}"
                for p in entries
            ]
            lines.append("providers: " + "; ".join(parts))

    others = sorted(str(k) for k in data if k not in ("mcp", "permissions", "providers"))
    if others:
        lines.append("also sets: " + ", ".join(others))
    return lines or ["no settings"]


def _permission_rules(perms: dict[str, Any]) -> list[tuple[str, str, str]]:
    rules: list[tuple[str, str, str]] = []
    for tool, entry in perms.items():
        if tool == "hardline":
            continue
        if isinstance(entry, dict):
            rules.extend((str(action), str(tool), str(pattern)) for pattern, action in entry.items())
        elif entry is not None:
            rules.append((str(entry), str(tool), "*"))
    return rules


def _short(text: str, limit: int = _MAX_TEXT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
