"""Modes plus the central decide() entry point.

Modes: ``default`` (ask edits + non-allowlisted bash), ``accept-edits``
(edits allowed, bash asks), ``plan`` (read-only + ``exit_plan``), ``auto``
(everything not denied allowed, side effects logged), ``yolo`` (everything
except hardline).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import hardline
from .rules import Action, Rule, evaluate, merge


class PermissionMode(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept-edits"
    PLAN = "plan"
    AUTO = "auto"
    YOLO = "yolo"
    # Legacy M0 aliases (kept so old configs keep working).
    ASK = "default"
    AUTO_EDIT = "accept-edits"

    @classmethod
    def _missing_(cls, value: object) -> PermissionMode | None:
        if not isinstance(value, str):
            return None
        legacy = {"ask": "default", "auto-edit": "accept-edits", "auto_edit": "accept-edits"}
        normalized = value.strip().lower()
        target = legacy.get(normalized) or normalized.replace("_", "-")
        for member in cls:
            if member.value == target:
                return member
        return None


class InvalidPermissionMode(ValueError):
    """A configured permission mode that is not a known name (a usage error for the CLI, a reply for the gateway)."""


def permission_mode_from_config(key: str, value: str) -> PermissionMode:
    """The PermissionMode for a config string; an unknown value raises InvalidPermissionMode naming the key."""
    try:
        return PermissionMode(value)
    except ValueError:
        raise InvalidPermissionMode(
            f"{key} {value!r} is not one of: ask, auto-edit, yolo (set in config.yaml or K3CODE_{key.upper()})"
        ) from None


#: Builtin read-only bash allowlist: safe to run without asking.
BUILTIN_BASH_ALLOW = [
    "ls *",
    "cat *",
    "grep *",
    "rg *",
    "git status *",
    "git diff *",
    "git log *",
]

#: ``rg --pre CMD`` runs CMD on every file; ``git diff|log --output=FILE`` writes FILE; ``--ext-diff``/``--textconv``
#: run configured external programs.
BUILTIN_BASH_ASK = [
    "rg* --pre*",
    "rg* --hostname-bin*",
    "git diff* --output*",
    "git log* --output*",
    "git diff* --ext-diff*",
    "git diff* --textconv*",
    "git log* --ext-diff*",
    "git log* --textconv*",
]

PURE_TOOLS = frozenset({"read", "grep", "glob", "todo", "skill", "mcp_tool_search", "task", "task_result"})
EDIT_TOOLS = frozenset({"write", "edit"})
READ_TOOLS = frozenset({"read", "grep", "glob"})  # path-taking read-only tools
EXIT_PLAN_TOOL = "exit_plan"
PLAN_MSG = "Plan mode is read-only; present the plan with exit_plan."


@dataclass
class Decision:
    """Outcome of decide(): what to do + what to show the user."""

    action: Action  # allow | ask | deny
    message: str | None = None  # denial reason (deny) or prompt hint (ask)
    patterns: list[str] = field(default_factory=list)  # evaluated patterns
    rule: Rule | None = None  # winning rule (or synthetic default)
    hardline: str | None = None  # hardline rule name, if denied by hardline
    auto_allowed: bool = False  # mode auto-allowed a would-be ask (log me)


def builtin_defaults() -> list[Rule]:
    """Builtin ruleset: pure tools + safe bash allow, writes/other bash ask."""
    rules: list[Rule] = [Rule(tool=t, pattern="*", action="allow") for t in sorted(PURE_TOOLS - READ_TOOLS)]
    rules += [Rule(tool="bash", pattern=p, action="allow") for p in BUILTIN_BASH_ALLOW]
    # Flags that make an allowed read-only command execute or write something: longer patterns win, so these ask.
    rules += [Rule(tool="bash", pattern=p, action="ask") for p in BUILTIN_BASH_ASK]
    rules.append(Rule(tool=EXIT_PLAN_TOOL, pattern="*", action="allow"))
    return rules


_RANK: dict[str, int] = {"allow": 1, "ask": 2, "deny": 3}


def _norm(path: str | Path) -> str:
    return os.path.normpath(os.path.expanduser(str(path)))


def _abs(raw: str, cwd: str) -> str:
    p = os.path.expanduser(raw or ".")
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def _inside(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def decide(
    *,
    mode: PermissionMode | str,
    tool: str,
    args: dict[str, object] | None = None,
    cwd: str | Path = ".",
    add_dirs: list[str] | None = None,
    user_rules: list[Rule] | None = None,
    project_rules: list[Rule] | None = None,
    session_rules: list[Rule] | None = None,
    hardline_extra: list[str] | None = None,
    headless: bool = False,
) -> Decision:
    """Decide allow/ask/deny for one tool call.

    ``cwd`` is the session working dir (relative paths resolve against it);
    ``add_dirs`` extend the readable/writable roots. Order: hardline deny,
    then mode (plan/yolo), then rules (builtin < user < project < session),
    then auto-mode conversion of ask, then headless conversion of ask.
    """
    mode = PermissionMode(mode)
    args = args or {}
    cwd_s = _norm(cwd)
    roots = [cwd_s, *[_norm(d) for d in (add_dirs or [])]]
    ruleset = merge(builtin_defaults(), user_rules or [], project_rules or [], session_rules or [])

    if tool == "bash":
        dec = _decide_bash(mode, str(args.get("command", "")), ruleset, hardline_extra, roots, cwd_s)
    elif tool in EDIT_TOOLS or tool in READ_TOOLS:
        dec = _decide_path(mode, tool, _abs(str(args.get("path") or args.get("file") or "."), cwd_s), roots, ruleset)
    elif tool == EXIT_PLAN_TOOL:
        dec = Decision(action="allow", patterns=["*"])
    else:
        base: Action = "deny" if mode == PermissionMode.PLAN else "ask"
        if tool in PURE_TOOLS:
            base = "allow"
        rule = evaluate(tool, "*", ruleset, default=base)
        dec = Decision(action=rule.action, patterns=["*"], rule=rule)
        if mode == PermissionMode.PLAN and tool not in PURE_TOOLS:
            dec.action, dec.message = "deny", PLAN_MSG

    return _finish(dec, mode, headless)


def _finish(dec: Decision, mode: PermissionMode, headless: bool) -> Decision:
    if dec.hardline is None and dec.action != "deny":
        if mode == PermissionMode.YOLO:
            dec.action = "allow"
        elif mode == PermissionMode.AUTO and dec.action == "ask":
            dec.action, dec.auto_allowed = "allow", True
    if dec.action == "ask" and headless:
        dec.action = "deny"
        dec.message = dec.message or "Permission denied: approval required but unavailable (headless mode)."
    if dec.action == "deny" and not dec.message:
        dec.message = f"Denied by rule {dec.rule.tool}:{dec.rule.pattern}" if dec.rule else "Denied."
    return dec


#: Redirections that are harmless next to any command (they do not write or read an arbitrary file).
_HARMLESS_REDIRECT = re.compile(r"(?:\d*[<>]&(?:\d+|-)|&>>?\s*/dev/null|\d*>>?&?\s*/dev/null)(?![^\s;&|<>()])")
#: ``>f``, ``>>f``, ``<f``, ``&>f``, ``&>>f``, ``>&f`` (the last three send stdout+stderr to f).
_REDIRECT = re.compile(r"(?:^|[^<>&\d])(?:&>>?|\d*(?:>>?|<)&?)\s*([^\s;&|<>()]+)")
_SUBSTITUTION = re.compile(r"\$\(|`")
#: Redirect targets that are code or credentials even inside the project.
_SENSITIVE_TARGET = re.compile(r"(?:^|/)(?:\.ssh|\.gnupg|\.git/(?:hooks|config)|\.k3code|\.aws|\.bash_?(?:rc|_profile)|"
                               r"\.zsh(?:rc|env)|\.profile|\.zprofile|authorized_keys|\.netrc|\.npmrc|\.env)(?:/|$)")


def _redirects_ok(plain: str, roots: list[str], cwd: str) -> bool:
    """Every redirect target is a plain path inside the project roots and not a credential/startup file."""
    for target in _REDIRECT.findall(plain):
        target = target.strip("'\"")
        if not target or any(ch in target for ch in "$*?[{~") and not target.startswith("~/"):
            return False  # a variable or glob: unknown target
        path = _abs(target, cwd)
        if not _inside(path, roots) or _SENSITIVE_TARGET.search(path):
            return False
    return True


def _voids_allow(sub: str, rule: Rule, ruleset: list[Rule], roots: list[str], cwd: str) -> bool:
    """A redirect or command substitution turns an allowed prefix into something else (``ls > ~/.bashrc``,
    ``git commit -m "$(evil)"``). Builtin read-only allows never cover either. A rule the user approved (session /
    project / user layer) still covers redirects into the project and substitutions whose contents are themselves
    allowed. Before, an "always allow git commit" also allowed ``git commit > ~/.bashrc`` and ``git commit -m
    "$(evil)"``."""
    plain = _HARMLESS_REDIRECT.sub("", sub)
    has_redirect = bool(_REDIRECT.search(plain))
    has_subst = bool(_SUBSTITUTION.search(plain))
    if rule.layer == 0:
        return has_redirect or has_subst
    if has_redirect and not _redirects_ok(plain, roots, cwd):
        return True
    if not has_subst:
        return False
    nested = hardline.parse(sub).nested
    return not nested or any(
        evaluate("bash", n, ruleset, default="ask").action != "allow" or hardline.is_launcher(n) for n in nested
    )


def _decide_bash(
    mode: PermissionMode, command: str, ruleset: list[Rule], extra: list[str] | None, roots: list[str], cwd: str
) -> Decision:
    hit = hardline.check(command, extra)
    if hit:
        return Decision(action="deny", message=f"Hardline deny ({hit}): {command[:120]}", hardline=hit)
    subs = hardline.split_commands(command)
    prefixes = [hardline.command_prefix(s) or s for s in subs]
    if mode == PermissionMode.PLAN:
        return Decision(action="deny", patterns=prefixes, message=PLAN_MSG)
    if mode == PermissionMode.YOLO:
        return Decision(action="allow", patterns=prefixes)
    worst: Rule | None = None
    for sub in subs:
        rule = evaluate("bash", sub, ruleset, default="ask")
        if rule.action == "allow" and _voids_allow(sub, rule, ruleset, roots, cwd):
            rule = Rule(tool="bash", pattern="*", action="ask")
        if worst is None or _RANK[rule.action] > _RANK[worst.action]:
            worst = rule
    worst = worst or Rule(tool="bash", pattern="*", action="ask")
    return Decision(action=worst.action, patterns=prefixes, rule=worst)


def _decide_path(mode: PermissionMode, tool: str, path: str, roots: list[str], ruleset: list[Rule]) -> Decision:
    is_edit = tool in EDIT_TOOLS
    if is_edit and mode == PermissionMode.PLAN:
        return Decision(action="deny", patterns=[path], message=PLAN_MSG)
    inside = _inside(path, roots)
    if not inside:
        fallback: Action = "ask"
    elif is_edit:
        fallback = "allow" if mode in (PermissionMode.ACCEPT_EDITS, PermissionMode.AUTO, PermissionMode.YOLO) else "ask"
    else:
        fallback = "allow"
    rule = evaluate("edit" if is_edit else "read", path, ruleset, default=fallback)
    dec = Decision(action=rule.action, patterns=[path], rule=rule)
    if not inside and rule.layer == 0:
        dec.message = f"Outside project roots: {path}"
    return dec


def suggest_rules(tool: str, dec: Decision) -> list[Rule]:
    """Narrowest rules to persist for an ``always``/``session`` approval."""
    if tool == "bash":
        # "<prefix> *" matches "git commit" and "git commit -m x" but not "git commit-tree"/"shutdown".
        # Never for launchers (shells, ssh, interpreters, xargs, find, sudo ...): "always allow `python3 *`" would
        # allow every command the user will ever be asked about.
        return [
            Rule(tool="bash", pattern=f"{p} *", action="allow")
            for p in dict.fromkeys(dec.patterns)
            if p and not hardline.is_launcher(p)
        ]
    name = "edit" if tool in EDIT_TOOLS else "read" if tool in READ_TOOLS else tool
    return [Rule(tool=name, pattern=p, action="allow") for p in dec.patterns]
