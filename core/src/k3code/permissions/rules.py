# Vendored from anomalyco/opencode@ecc4916b5a9608c30e6dd58a67f2137b594407ca:
# packages/opencode/src/permission/index.ts, fromConfig/expand/merge (MIT).
# Ported to Python.
"""Permission rules: shape, config parsing, evaluation.

A rule is ``{"tool": <tool>, "pattern": <wildcard>, "action": <action>}``
where action is ``allow`` | ``ask`` | ``deny`` and ``tool`` follows the
opencode ``permission`` vocabulary: ``read``, ``edit``, ``bash`` for the
gated tools, any other tool name for the rest.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from . import wildcard

Action = Literal["allow", "ask", "deny"]

ACTION_RANK: dict[str, int] = {"allow": 1, "ask": 2, "deny": 3}
#: Rule.layer values as assigned by merge(builtin, user, project, session).
USER_LAYER, PROJECT_LAYER, SESSION_LAYER = 1, 2, 3


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str
    action: Action
    layer: int = field(default=0, compare=False)  # source: 0 builtin, 1 user, 2 project, 3 session

    def __post_init__(self) -> None:
        if self.action not in ("allow", "ask", "deny"):
            raise ValueError(f"invalid action: {self.action!r}")


def expand(pattern: str) -> str:
    """Expand leading ``~`` or ``$HOME`` in a config pattern."""
    home = os.path.expanduser("~")
    if pattern == "~":
        return home
    if pattern.startswith("~/"):
        return home + pattern[1:]
    if pattern == "$HOME":
        return home
    if pattern.startswith("$HOME/"):
        return home + pattern[5:]
    return pattern


#: Claude Code's tool names (lower case) -> the tools rules name here (the opencode vocabulary: write and edit are
#: both ``edit``).
_CLAUDE_TOOLS = {
    "bash": "bash",
    "read": "read",
    "grep": "read",  # the engine decides every read-only file tool as `read`
    "glob": "read",
    "edit": "edit",
    "write": "edit",
    "multiedit": "edit",
    "notebookedit": "edit",
    "webfetch": "web_fetch",
    "websearch": "web_search",
}
#: Tools whose rules match absolute paths.
_PATH_TOOLS = frozenset({"read", "edit"})
_SPECIFIER = re.compile(r"^\s*([A-Za-z_][\w.*-]*)\s*(?:\((.*)\))?\s*$", re.DOTALL)
#: Claude Code's rule lists in a ``permissions:`` mapping.
RULE_LISTS = ("allow", "ask", "deny")


def parse_specifier(spec: str) -> tuple[str, str] | None:
    """Claude Code's ``Tool(argument)`` as ``(tool, pattern)``: ``Bash(git push:*)`` is ``bash`` with ``git push *``
    (the ``:*`` suffix is a prefix match), ``Bash(npm run test)`` that exact command, ``Bash`` or ``Bash(*)`` every
    command, ``Read(~/.zshrc)`` / ``Edit(src/**)`` the path pattern, ``WebFetch(domain:example.com)`` that host,
    ``mcp__server__tool`` that tool and ``mcp__server`` all of the server's. A tool this does not know (``Task``,
    ``TodoWrite``) keeps its own lower-cased name: a rule for a tool that does not exist here is harmless, and the
    same rule would otherwise be a deny of everything. None when it is not a specifier at all."""
    m = _SPECIFIER.match(spec)
    if not m:
        return None
    name, arg = m.group(1), (m.group(2) or "").strip()
    if m.group(2) is not None and not arg:
        return None  # `Bash()` names nothing: not "every command"
    if name.startswith("mcp__"):
        tool = name if name.count("__") > 1 else name + "__*"
    else:
        tool = _CLAUDE_TOOLS.get(name.lower(), name.lower())
    if tool == "bash" and arg.endswith(":*"):
        prefix = arg[:-2].rstrip()
        arg = prefix + " *" if prefix else "*"
    elif tool == "web_fetch" and arg.lower().startswith("domain:"):
        arg = arg[len("domain:") :].strip()
    return tool, arg or "*"


def _anchored(tool: str, pattern: str, action: str) -> str | None:
    """A path rule as the engine matches it, against absolute paths, read as Claude Code reads it: ``//path`` is the
    absolute ``/path``, ``~/x`` and ``$HOME/x`` are the home, and anything else (``src/**``, ``.env``, ``./x``, and
    ``/src/**``, which is relative to the settings file there) is relative to a project. A project cannot be pinned
    here, so an ask or deny matches such a pattern in any directory (stricter, never looser) and an allow is refused
    (None): it would open the same name everywhere."""
    if tool not in _PATH_TOOLS or pattern.startswith(("~", "$")):
        return pattern
    if pattern.startswith("//"):
        return pattern[1:]
    if pattern in ("*", "**"):
        return "*"  # the whole tool (`Read`, `Edit(*)`): an allow of it is meant
    if action == "allow":
        return None  # also a pattern that starts with `*` (`**/*.py`): it would open that name in every directory
    return pattern if pattern.startswith("*") else "*/" + pattern.lstrip("/").removeprefix("./")


def from_config(permissions: dict[str, Any], problems: list[str] | None = None) -> list[Rule]:
    """Parse a ``permissions:`` config mapping into a ruleset.

    ``{bash: "ask"}`` -> rule with pattern ``*``; ``{bash: {"git *": "allow"}}``
    -> one rule per entry, in insertion order (later overrides earlier). Claude Code's lists are read too:
    ``{allow: ["Bash(git push:*)"], deny: ["Read(~/.ssh/**)"]}`` (see :func:`parse_specifier`); a specifier that cannot
    be read is a deny of everything when it sits in ``ask`` or ``deny`` (what it meant to stop is unknown) and dropped
    when it sits in ``allow`` (a dropped allow opens nothing), reported either way.

    An invalid entry raises ``ValueError``. Given ``problems`` it fails closed instead: the entry becomes a ``deny``
    over everything it could have meant (its pattern; the whole tool when the pattern or the tool's value cannot be
    read), one line naming it (never its pattern, which may hold a secret) goes to ``problems``, and the other entries
    are kept (dropping them dropped the user's deny rules, and auto/yolo then allowed what they denied).
    """
    ruleset: list[Rule] = []

    def invalid(tool: str, pattern: str, where: str, why: str) -> None:
        if problems is None:
            raise ValueError(f"permissions.{where}: {why}")
        problems.append(f"permissions.{where}: {why}; enforced as deny")
        ruleset.append(Rule(tool=tool, pattern=pattern, action="deny"))

    for key, value in permissions.items():
        tool = str(key)
        if tool in RULE_LISTS and isinstance(value, list):
            for n, spec in enumerate(value, 1):
                parsed = parse_specifier(spec) if isinstance(spec, str) else None
                anchored = _anchored(parsed[0], parsed[1], tool) if parsed else None  # before ~ becomes a path
                if parsed is not None and anchored is not None:
                    ruleset.append(Rule(tool=parsed[0], pattern=expand(anchored), action=cast(Action, tool)))
                    if (
                        tool != "allow"
                        and parsed[0] in _PATH_TOOLS
                        and not re.search(r"[*?\[]", anchored.rsplit("/", 1)[-1])
                    ):
                        # a name that is a directory (`Read(secrets)`) stops what is inside it too, as in gitignore
                        ruleset.append(
                            Rule(tool=parsed[0], pattern=expand(anchored).rstrip("/") + "/*", action=cast(Action, tool))
                        )
                elif tool == "allow":
                    why = "not a Tool(argument) rule, or a path rule that is not absolute (use //abs, ~/ or $HOME/)"
                    if problems is None:
                        raise ValueError(f"permissions.allow entry {n}: {why}")
                    problems.append(f"permissions.allow entry {n}: {why}; ignored")
                else:
                    invalid("*", "*", f"{tool} entry {n}", "not a Tool(argument) rule")
            continue
        entries = {"*": value} if isinstance(value, str) else value
        if not isinstance(entries, dict):
            invalid(tool, "*", tool, "must be an action or a pattern→action mapping")
            continue
        for n, (pattern, action) in enumerate(entries.items(), 1):
            where = tool if entries is not value else f"{tool} entry {n}"
            if not isinstance(pattern, str):
                invalid(tool, "*", where, "the pattern is not text (quote it)")
            elif action not in ("allow", "ask", "deny"):
                invalid(tool, expand(pattern), where, "the action is not allow, ask or deny")
            else:
                ruleset.append(Rule(tool=tool, pattern=expand(pattern), action=action))
    return ruleset


def merge(*rulesets: list[Rule]) -> list[Rule]:
    """Concatenate rulesets (builtin < user < project < session).

    Rules already stamped with their layer skip the rebuild: builtin rules come
    from a cached tuple at layer 0, so a hot-path decide() avoids re-creating
    ~150 Rule objects per call.
    """
    merged: list[Rule] = []
    for layer, ruleset in enumerate(rulesets):
        for r in ruleset:
            merged.append(r if r.layer == layer else replace(r, layer=layer))
    return merged


def evaluate(tool: str, pattern: str, ruleset: list[Rule], *, default: Action = "ask") -> Rule:
    """Pick the winning rule for ``(tool, pattern)``.

    Most specific pattern wins; ties go to the later source (session > project >
    user > builtin), then deny > ask > allow. Falls back to ``{"tool": tool, "pattern": "*", action: default}``.
    User config wins over a project's: a project allow never overrides a matching user (or session) deny, however
    specific it is (a cloned repo's ``git push origin *: allow`` beat the user's ``git push *: deny``).
    Session outranks user by design (pinned by test): the session is the user's live intent for this run,
    while project repos are untrusted third parties.
    """
    best: Rule | None = None
    best_key: tuple[int, ...] = (-1, -1, -1, -1)
    vetoes: list[tuple[tuple[int, ...], Rule]] = []  # user/session denies that a project allow must not override
    for index, rule in enumerate(ruleset):
        if not wildcard.match(tool, rule.tool):
            continue
        if rule.tool == "*" and rule.pattern == "*":
            specificity = 0
        elif rule.tool == "*":
            specificity = len(rule.pattern)
        else:
            specificity = 100_000 + len(rule.pattern)
        if not wildcard.match(pattern, rule.pattern):
            continue
        key = (specificity, rule.layer, ACTION_RANK[rule.action], index)
        if rule.action == "deny" and rule.layer in (USER_LAYER, SESSION_LAYER):
            vetoes.append((key, rule))
        if key >= best_key:
            best, best_key = rule, key
    if best is None:
        return Rule(tool=tool, pattern="*", action=default)
    if best.action == "allow" and best.layer == PROJECT_LAYER and vetoes:
        return max(vetoes, key=lambda v: v[0])[1]
    return best
