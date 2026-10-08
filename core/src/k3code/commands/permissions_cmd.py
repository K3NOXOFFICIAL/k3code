"""/permissions: show mode + effective rules by source, change mode, add/remove rules (suggest: learning_cmd)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from k3code.commands._util import pop_flag, reply, session_cwd
from k3code.paths import project_config_path, user_config_path
from k3code.permissions.engine import PermissionMode, builtin_defaults
from k3code.permissions.hardline import HARDLINE_NAMES
from k3code.permissions.rules import Rule, expand
from k3code.permissions.state import PermissionState, persist_rules

USAGE = (
    "Usage: /permissions | mode <default|accept-edits|plan|auto|yolo> | "
    "allow|ask|deny <tool> <pattern> [--project|--user|--session] | rm <id> | suggest"
)
_SRC = {"builtin": "b", "user": "u", "project": "p", "session": "s"}


def _layers(perms: PermissionState) -> list[tuple[str, list[Rule]]]:
    return [("builtin", builtin_defaults()), ("user", perms.user_rules), ("project", perms.project_rules),
            ("session", perms.session_rules)]


def effective_rules(perms: PermissionState) -> list[tuple[str, str, Rule]]:
    """(id, source, rule) for every rule, ids like ``u1``/``p2``/``s1``/``b3`` (1-based within its source)."""
    return [(f"{_SRC[src]}{i}", src, r) for src, rules in _layers(perms) for i, r in enumerate(rules, 1)]


def format_overview(perms: PermissionState) -> str:
    lines = [f"Mode: {perms.mode.value}", "", "Rules (later sources override earlier ones):"]
    for rid, src, r in effective_rules(perms):
        lines.append(f"  {rid:<4} {r.action:<5} {r.tool} {r.pattern}  [{src}]")
    lines += ["", "Hardline (always denied, any mode):"]
    lines += [f"  - {name}" for name in HARDLINE_NAMES]
    lines += [f"  - (config) {h}" for h in perms.hardline_extra]
    return "\n".join(lines)


def _remove_from_config(path: Path, rule: Rule) -> bool:
    if not path.is_file():
        return False
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = (data.get("permissions") or {}).get(rule.tool)
    if isinstance(section, str):
        if rule.pattern != "*" or section != rule.action:
            return False
        del data["permissions"][rule.tool]
    elif isinstance(section, dict):
        hit = next((k for k, v in section.items() if expand(str(k)) == rule.pattern and v == rule.action), None)
        if hit is None:
            return False
        del section[hit]
        if not section:
            del data["permissions"][rule.tool]
    else:
        return False
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return True


def handle(ctx: Any, session_id: str | None, args: list[str]) -> dict[str, Any] | None:
    """Handle every sub-command except ``suggest``; None = not mine."""
    live = ctx.sessions.get(session_id) if session_id else None
    perms: PermissionState = live.perms if live is not None else PermissionState(cwd=session_cwd(ctx, session_id))
    perms.reload()
    sub = args[0] if args else ""
    if not sub or sub == "list":
        return reply(format_overview(perms))
    if sub == "mode":
        if len(args) < 2:
            return reply(f"Mode: {perms.mode.value}\n{USAGE}")
        try:
            mode = PermissionMode(args[1])
        except ValueError:
            mode = None
        if mode is None:
            return reply(f"Unknown mode: {args[1]}")
        if live is None:
            return reply("No active session to change the mode of.")
        live.set_mode(mode)
        return reply(f"Permission mode: {mode.value}")
    if sub in ("allow", "ask", "deny"):
        rest = args[1:]
        project, user, session = (pop_flag(rest, f) for f in ("--project", "--user", "--session"))
        if len(rest) < 2:
            return reply(USAGE)
        tool, pattern = rest[0], " ".join(rest[1:])
        rule = Rule(tool=tool, pattern=expand(pattern), action=sub)  # type: ignore[arg-type]
        if session:
            perms.session_rules.append(rule)
            where = "session"
        else:
            path = user_config_path() if user and not project else project_config_path(perms.cwd)
            persist_rules(path, [rule])
            perms.reload()
            where = f"{'user' if path == user_config_path() else 'project'} ({path})"
        return reply(f"{sub} {tool} {pattern} added to {where}.")
    if sub == "rm":
        if len(args) < 2:
            return reply("Usage: /permissions rm <id>  (ids from /permissions)")
        by_id = {rid: (src, r) for rid, src, r in effective_rules(perms)}
        found = by_id.get(args[1])
        if found is None:
            return reply(f"No rule {args[1]}.")
        src, rule = found
        if src == "builtin":
            return reply("Built-in rules cannot be removed; add an overriding rule instead.")
        if src == "session":
            perms.session_rules.remove(rule)
            return reply(f"Removed {args[1]}.")
        path = user_config_path() if src == "user" else project_config_path(perms.cwd)
        if not _remove_from_config(path, rule):
            return reply(f"{args[1]} was not found in {path}.")
        perms.reload()
        return reply(f"Removed {args[1]} from {path}.")
    return None
