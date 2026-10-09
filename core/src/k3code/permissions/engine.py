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
    #: an ask that auto mode must not convert: a human answers it, an unattended run treats it as a deny
    needs_human: bool = False


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
    """``path`` lies under one of ``roots``, after resolving symlinks on both sides: a lexical check let
    ``proj/docs -> ../outside`` turn a write to ``docs/x`` into a write outside the project. A path that does not
    exist yet resolves through its nearest existing parent (realpath keeps the missing remainder as is)."""
    real = os.path.realpath(path)
    real_roots = [os.path.realpath(r) for r in roots]
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in real_roots)


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
        # The command runs in args["cwd"] (relative to the session cwd): redirects resolve against it.
        bash_cwd = _abs(str(args.get("cwd") or "."), cwd_s)
        dec = _decide_bash(mode, str(args.get("command", "")), ruleset, hardline_extra, roots, bash_cwd)
    elif tool in EDIT_TOOLS or tool in READ_TOOLS:
        path = _abs(str(args.get("path") or args.get("file") or "."), cwd_s)
        if tool == "glob":
            path = _glob_base(path, str(args.get("pattern") or ""))
        dec = _decide_path(mode, tool, path, roots, ruleset)
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
        elif mode == PermissionMode.AUTO and dec.action == "ask" and not dec.needs_human:
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
#: Output redirections only (``>``, ``>>``, ``&>``, ``&>>``, ``>&``): reading with ``<`` is never restricted by the
#: write roots.
_WRITE_REDIRECT = re.compile(r"(?:^|[^<>&\d])(?:&>>?|\d*>>?&?)\s*([^\s;&|<>()]+)")
_SUBSTITUTION = re.compile(r"\$\(|`")
#: Redirect targets that are code or credentials even inside the project.
_SENSITIVE_TARGET = re.compile(
    r"(?:^|/)(?:\.ssh|\.gnupg|\.git/(?:hooks|config)|\.k3code|\.aws|\.bash_?(?:rc|_profile)|"
    r"\.zsh(?:rc|env)|\.profile|\.zprofile|authorized_keys|\.netrc|\.npmrc|\.env)(?:/|$)"
)


def _writes_outside_roots(sub: str, roots: list[str], cwd: str) -> bool:
    """A redirect in ``sub`` writes a file outside the project roots. Targets that are variables or globs are left to
    the sandbox; the ``/dev`` sinks are not files."""
    plain = _HARMLESS_REDIRECT.sub("", sub)
    for target in _WRITE_REDIRECT.findall(plain):
        target = target.strip("'\"")
        if not target or target.startswith("/dev/") or any(ch in target for ch in "$`*?[{"):
            continue
        if not _inside(_abs(target, cwd), roots):
            return True
    return False


def _redirects_ok(plain: str, roots: list[str], cwd: str) -> bool:
    """Every redirect target is a plain path inside the project roots and not a credential/startup file."""
    for target in _REDIRECT.findall(plain):
        target = target.strip("'\"")
        if not target or any(ch in target for ch in "$*?[{~") and not target.startswith("~/"):
            return False  # a variable or glob: unknown target
        path = _abs(target, cwd)
        if not _inside(path, roots) or any(_SENSITIVE_TARGET.search(p) for p in (path, os.path.realpath(path))):
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
        # the builtin read-only allowlist covers the project only: `cat /etc/x`, `grep -r k ~` and `ls $DIR` ask
        return has_redirect or has_subst or any(_arg_outside(a, roots, cwd) for a in _path_args(sub))
    if has_redirect and not _redirects_ok(plain, roots, cwd):
        return True
    if not has_subst:
        return False
    nested = hardline.parse(sub).nested
    return not nested or any(
        evaluate("bash", n, ruleset, default="ask").action != "allow" or hardline.is_launcher(n) for n in nested
    )


#: Read-only commands (the builtin allowlist and its kin) that must never be pointed at a key file.
_READERS = frozenset({"cat", "grep", "egrep", "fgrep", "rg", "ls", "head", "tail", "less", "more"})


def _path_args(sub: str) -> list[str]:
    """The non-flag arguments of ``sub`` (after sudo/env/... wrappers), plus the values of ``--flag=value``."""
    words = _REDIRECT.sub(" ", _HARMLESS_REDIRECT.sub("", sub))  # redirect targets are checked by _redirects_ok
    real = hardline.strip_wrappers(hardline.tokens(words))
    out: list[str] = []
    for t in real[1:]:
        if t.startswith("-"):
            if "=" in t:
                out.append(t.split("=", 1)[1])
            continue
        out.append(t)
    return out


def _arg_outside(arg: str, roots: list[str], cwd: str) -> bool:
    if not arg:
        return False
    if arg.startswith("$"):
        return True  # a variable: an unknown path
    return not _inside(_abs(arg, cwd), roots)


def _reads_secret(sub: str, cwd: str) -> bool:
    real = hardline.strip_wrappers(hardline.tokens(sub))
    if not real or os.path.basename(real[0]) not in _READERS:
        return False
    return any(sensitive_path(_abs(a, cwd)) for a in _path_args(sub) if a and not a.startswith("$"))


def _decide_bash(
    mode: PermissionMode, command: str, ruleset: list[Rule], extra: list[str] | None, roots: list[str], cwd: str
) -> Decision:
    hit = hardline.check(command, extra, cwd=cwd)
    if hit:
        return Decision(action="deny", message=f"Hardline deny ({hit}): {command[:120]}", hardline=hit)
    parsed = hardline.parse(command)
    subs = parsed.subs
    # an open quote or a quote inside a comment: the shell may not run what was parsed, so no rule vouches for it
    unsafe = parsed.unterminated or parsed.comment_quote
    if any(_reads_secret(sub, cwd) for sub in subs):
        hit = "sensitive-path"
        return Decision(action="deny", message=f"Hardline deny ({hit}): {command[:120]}", hardline=hit)
    prefixes = [hardline.command_prefix(s) or s for s in subs]
    if mode == PermissionMode.PLAN:
        return Decision(action="deny", patterns=prefixes, message=PLAN_MSG)
    if mode == PermissionMode.YOLO:
        return Decision(action="allow", patterns=prefixes)
    if mode == PermissionMode.AUTO and any(_writes_outside_roots(sub, roots, cwd) for sub in subs):
        # auto mode writes only inside the project roots; no allow rule overrides this
        return Decision(
            action="deny", patterns=prefixes, message=f"Auto mode writes only inside the project roots: {command[:120]}"
        )
    worst: Rule | None = None
    for sub in subs:
        rule = evaluate("bash", sub, ruleset, default="ask")
        if rule.action == "allow" and _voids_allow(sub, rule, ruleset, roots, cwd):
            rule = Rule(tool="bash", pattern="*", action="ask")
        if rule.action == "allow" and rule.layer == 0 and "\n" in sub:
            unsafe = True  # the builtin read-only allowlist covers one-line commands only
        if worst is None or _RANK[rule.action] > _RANK[worst.action]:
            worst = rule
    worst = worst or Rule(tool="bash", pattern="*", action="ask")
    if unsafe and worst.action != "deny":
        return Decision(
            action="ask",
            patterns=prefixes,
            needs_human=True,
            message=f"Command could not be parsed safely; confirm it yourself: {command[:120]}",
        )
    if worst.action == "allow" and not _inside(cwd, roots):
        return Decision(action="ask", patterns=prefixes, message=f"Working directory outside project roots: {cwd}")
    return Decision(action=worst.action, patterns=prefixes, rule=worst)


def _decide_path(mode: PermissionMode, tool: str, path: str, roots: list[str], ruleset: list[Rule]) -> Decision:
    is_edit = tool in EDIT_TOOLS
    if is_edit and mode == PermissionMode.PLAN:
        return Decision(action="deny", patterns=[path], message=PLAN_MSG)
    if not is_edit and sensitive_path(path):
        # read/grep/glob run in the daemon process, outside the sandbox: they would hand the keys to the model
        return Decision(
            action="deny", patterns=[path], hardline="sensitive-path", message=f"Hardline deny (sensitive-path): {path}"
        )
    inside = _inside(path, roots)
    if not inside:
        fallback: Action = "ask"
    elif is_edit:
        fallback = "allow" if mode in (PermissionMode.ACCEPT_EDITS, PermissionMode.AUTO, PermissionMode.YOLO) else "ask"
    else:
        fallback = "allow"
    rule = evaluate("edit" if is_edit else "read", path, ruleset, default=fallback)
    dec = Decision(action=rule.action, patterns=[path], rule=rule)
    if not inside:
        if rule.layer == 0:
            dec.message = f"Outside project roots: {path}"
        if is_edit and mode == PermissionMode.AUTO and rule.action == "ask":
            # auto mode writes only inside the project roots; an explicit user allow rule still applies
            dec.action, dec.message = "deny", f"Auto mode writes only inside the project roots: {path}"
        elif not is_edit and rule.action == "ask":
            # an in-process read outside the roots is never auto-allowed: a person approves it (or adds a read rule)
            dec.needs_human = True
    return dec


#: ``$HOME`` entries that hold keys or credentials (the sandbox masks the first two, see sandbox.HOME_SECRETS).
_HOME_SECRETS = (".config/k3code", ".ssh", ".gnupg", ".aws")
_PROC_ENVIRON = re.compile(r"^/proc/[^/]+/(?:task/[^/]+/)?environ$")
_DOTENV_TEMPLATES = (".example", ".sample", ".template", ".dist")


def _secret_dirs() -> list[str]:
    home = os.path.expanduser("~")
    dirs = [os.path.join(home, s) for s in _HOME_SECRETS]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.append(os.path.join(xdg, "k3code"))
    return dirs


def sensitive_path(path: str) -> bool:
    """``path`` (absolute) is a key/credential location: ``~/.ssh``, ``~/.gnupg``, ``~/.aws``, the k3code secrets dir
    (``~/.config/k3code``, ``$XDG_CONFIG_HOME/k3code``: the daemon's ``env`` file), a process environment
    (``/proc/*/environ``) or a dotenv file. Checked on the path as written and on its realpath (symlinks)."""
    dirs = [os.path.normpath(d) for d in _secret_dirs()]
    dirs += [os.path.realpath(d) for d in dirs]
    for p in dict.fromkeys((os.path.normpath(path), os.path.realpath(path))):
        if _PROC_ENVIRON.match(p) or any(p == d or p.startswith(d.rstrip("/") + "/") for d in dirs):
            return True
        name = os.path.basename(p)
        if name == ".env" or (name.startswith(".env.") and not name.endswith(_DOTENV_TEMPLATES)):
            return True
    return False


def _glob_base(path: str, pattern: str) -> str:
    """The directory a glob of ``pattern`` under ``path`` starts from: its literal leading segments count, so a
    pattern such as ``../../.ssh/*`` is decided as ``~/.ssh``."""
    if os.path.isabs(pattern):
        return os.path.normpath(pattern)  # pathlib refuses it; decide the absolute prefix anyway
    literal: list[str] = []
    for seg in pattern.split("/"):
        if any(ch in seg for ch in "*?["):
            break
        literal.append(seg)
    return os.path.normpath(os.path.join(path, *literal)) if literal else path


def _drop_assignments(prefix: str) -> str:
    """``prefix`` without its leading ``NAME=value`` words (environment assignments, often secrets)."""
    words = prefix.split(" ")
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words.pop(0)
    return " ".join(words)


def suggest_rules(tool: str, dec: Decision) -> list[Rule]:
    """Narrowest rules to persist for an ``always``/``session`` approval."""
    if tool == "bash":
        # "<prefix> *" matches "git commit" and "git commit -m x" but not "git commit-tree"/"shutdown".
        # Never for launchers (shells, ssh, interpreters, xargs, find, sudo ...): "always allow `python3 *`" would
        # allow every command the user will ever be asked about.
        # A leading VAR=value is dropped: "always" on `API_KEY=sk-... curl x` wrote the key into config.yaml.
        prefixes = (_drop_assignments(p) for p in dec.patterns)
        return [
            Rule(tool="bash", pattern=f"{p} *", action="allow")
            for p in dict.fromkeys(prefixes)
            if p and not hardline.is_launcher(p)
        ]
    name = "edit" if tool in EDIT_TOOLS else "read" if tool in READ_TOOLS else tool
    return [Rule(tool=name, pattern=p, action="allow") for p in dec.patterns]
