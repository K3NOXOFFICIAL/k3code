"""Per-session permission state, config loading and persistence of approvals."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .engine import Decision, PermissionMode, decide
from .rules import Rule, from_config

MODE_CYCLE = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.PLAN, PermissionMode.AUTO]


def _home() -> Path:
    return Path(os.environ.get("K3CODE_HOME", str(Path.home() / ".k3code"))).expanduser()


def load_permissions_config(path: Path) -> tuple[list[Rule], list[str]]:
    """Read ``permissions:`` from a config.yaml: (rules, extra hardline regexes)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        return [], []
    section = (data or {}).get("permissions") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return [], []
    section = dict(section)
    hard = [str(h) for h in section.pop("hardline", None) or []]
    try:
        return from_config(section), hard
    except (ValueError, AttributeError):
        return [], hard


def project_config_path(cwd: str | Path) -> Path:
    return Path(cwd) / ".k3code" / "config.yaml"


def persist_rules(path: Path, rules: list[Rule]) -> None:
    """Merge ``allow`` rules into ``permissions:`` of a config.yaml (created if missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    perms = data.setdefault("permissions", {})
    for r in rules:
        entry = perms.get(r.tool)
        if not isinstance(entry, dict):
            entry = {} if entry is None else {"*": entry}
            perms[r.tool] = entry
        entry[r.pattern] = r.action
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def log_decision(
    *, session: str, tool: str, pattern: str, choice: str, cwd: str, home: Path | None = None
) -> None:
    """Append one approval decision to ``$K3CODE_HOME/decisions.jsonl`` (learning hook for M5)."""
    path = (home or _home()) / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"session": session, "tool": tool, "pattern": pattern, "choice": choice, "cwd": cwd, "ts": time.time()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


@dataclass
class PermissionState:
    """Mutable permission context shared by a session and its AgentLoop."""

    mode: PermissionMode = PermissionMode.DEFAULT
    cwd: Path = field(default_factory=Path.cwd)
    add_dirs: list[str] = field(default_factory=list)
    session_rules: list[Rule] = field(default_factory=list)
    user_config: Path | None = None  # default: $K3CODE_HOME/config.yaml
    user_rules: list[Rule] = field(default_factory=list)
    project_rules: list[Rule] = field(default_factory=list)
    hardline_extra: list[str] = field(default_factory=list)

    def reload(self) -> None:
        """Re-read user + project config (cheap; called per turn)."""
        user, user_hard = load_permissions_config(self.user_config or _home() / "config.yaml")
        project, project_hard = load_permissions_config(project_config_path(self.cwd))
        self.user_rules, self.project_rules = user, project
        self.hardline_extra = [*user_hard, *project_hard]

    def decide(self, tool: str, args: dict[str, object], *, headless: bool = False) -> Decision:
        return decide(
            mode=self.mode,
            tool=tool,
            args=args,
            cwd=self.cwd,
            add_dirs=self.add_dirs,
            user_rules=self.user_rules,
            project_rules=self.project_rules,
            session_rules=self.session_rules,
            hardline_extra=self.hardline_extra,
            headless=headless,
        )

    def cycle_mode(self) -> PermissionMode:
        """default → accept-edits → plan → auto → default (yolo is never cycled into)."""
        try:
            nxt = MODE_CYCLE[(MODE_CYCLE.index(self.mode) + 1) % len(MODE_CYCLE)]
        except ValueError:
            nxt = PermissionMode.DEFAULT
        self.mode = nxt
        return nxt
