"""Per-session permission state, config loading and persistence of approvals."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from k3code import trust

from .engine import Decision, PermissionMode, decide
from .rules import Rule, from_config

MODE_CYCLE = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.PLAN, PermissionMode.AUTO]
logger = logging.getLogger(__name__)


def _home() -> Path:
    return Path(os.environ.get("K3CODE_HOME", str(Path.home() / ".k3code"))).expanduser()


#: What a config whose ``permissions:`` cannot be read at all contributes: a deny for every call that no more specific
#: rule decides (the builtin read-only allowlist still applies). Contributing nothing let auto allow what it denied.
UNREADABLE: tuple[Rule, ...] = (Rule(tool="*", pattern="*", action="deny"),)
_warned: set[tuple[str, str]] = set()


def load_permissions_config(path: Path) -> tuple[list[Rule], list[str]]:
    """Read ``permissions:`` from a config.yaml: (rules, extra hardline regexes). Malformed content fails closed."""
    rules, hard, problems = read_permissions(path)
    _warn_once(str(path), problems)
    return rules, hard


def permissions_from_text(text: str | None, source: str = "config") -> tuple[list[Rule], list[str]]:
    """The ``permissions:`` section of config text: (rules, extra hardline regexes). Malformed content fails closed."""
    rules, hard, problems = parse_permissions(text)
    _warn_once(source, problems)
    return rules, hard


def read_permissions(path: Path) -> tuple[list[Rule], list[str], list[str]]:
    """:func:`parse_permissions` of a config.yaml; a missing file has no rules, an unreadable one fails closed."""
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeDecodeError):
        return list(UNREADABLE), [], ["cannot read the file; every call no rule allows is denied"]
    return parse_permissions(text)


def parse_permissions(text: str | None) -> tuple[list[Rule], list[str], list[str]]:
    """(rules, extra hardline regexes, problems) of config text.

    Fails closed and says so in ``problems``: an invalid entry is a deny (``from_config``), and text whose
    ``permissions:`` cannot be read at all gives :data:`UNREADABLE`. No text, or no ``permissions:``, has no rules.
    """
    try:
        data = yaml.safe_load(text) if text is not None else None
    except yaml.YAMLError:
        return list(UNREADABLE), [], ["not valid YAML; every call no rule allows is denied"]
    if data is not None and not isinstance(data, dict):
        return list(UNREADABLE), [], ["the file is not a mapping; every call no rule allows is denied"]
    section = (data or {}).get("permissions")
    if section is None:
        return [], [], []
    if not isinstance(section, dict):
        return list(UNREADABLE), [], ["permissions: is not a mapping; every call no rule allows is denied"]
    section = dict(section)
    problems: list[str] = []
    hard: list[str] = []
    for i, h in enumerate(section.pop("hardline", None) or []):
        if str(h).strip():
            hard.append(str(h))
        else:  # '' matches every command: one stray empty entry denied all bash
            problems.append(f"permissions.hardline[{i}] is blank: ignored (it would match every command)")
    return from_config(section, problems), hard, problems


def _warn_once(source: str, problems: list[str]) -> None:
    """Log each problem once per process: the config is re-read on every turn."""
    for problem in problems:
        if (source, problem) not in _warned:
            _warned.add((source, problem))
            logger.warning("%s: %s (k3code doctor lists every entry)", source, problem)


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
    with trust.keeping_trust(path):
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def log_decision(*, session: str, tool: str, pattern: str, choice: str, cwd: str, home: Path | None = None) -> None:
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
    #: The project whose config and accepted rules apply (None = cwd): the CLI's --config-dir, a worktree child's
    #: parent project (its cwd is a fresh path under the one the user trusted).
    project_dir: Path | None = None
    #: Set by reload(). decide() reloads first while it is unset: a loop built without a reload (`k3code -p`, the
    #: REPL, a sub-agent) applied none of the configured deny rules and hardline patterns.
    loaded: bool = field(default=False, init=False, repr=False)

    def reload(self) -> None:
        """Re-read user + project config (cheap; called per turn)."""
        user, user_hard = load_permissions_config(self.user_config or _home() / "config.yaml")
        # Rules the user accepted from a project recipe live in k3code's state for this project, not in the repo:
        # they rank as the user's own (k3code.learning.projectstate.rules_path).
        from k3code.learning.projectstate import rules_path

        project_dir = self.project_dir or self.cwd
        accepted, _ = load_permissions_config(rules_path(project_dir))
        user = [*user, *accepted]
        # A project config the user has not trusted contributes no rules and no hardline patterns.
        text = trust.trusted_text(project_dir)
        project, project_hard = (
            permissions_from_text(text, str(project_config_path(project_dir))) if text is not None else ([], [])
        )
        self.user_rules, self.project_rules = user, project
        self.hardline_extra = [*user_hard, *project_hard]
        self.loaded = True

    def decide(self, tool: str, args: dict[str, object], *, headless: bool = False) -> Decision:
        if not self.loaded:
            self.reload()
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
