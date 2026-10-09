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
from dataclasses import dataclass, field, replace
from typing import Any, Literal

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


def from_config(permissions: dict[str, Any]) -> list[Rule]:
    """Parse a ``permissions:`` config mapping into a ruleset.

    ``{bash: "ask"}`` -> rule with pattern ``*``; ``{bash: {"git *": "allow"}}``
    -> one rule per entry, in insertion order (later overrides earlier).
    """
    ruleset: list[Rule] = []
    for tool, value in permissions.items():
        if isinstance(value, str):
            ruleset.append(Rule(tool=tool, pattern="*", action=value))
            continue
        for pattern, action in value.items():
            ruleset.append(Rule(tool=tool, pattern=expand(pattern), action=action))
    return ruleset


def merge(*rulesets: list[Rule]) -> list[Rule]:
    """Concatenate rulesets (builtin < user < project < session)."""
    merged: list[Rule] = []
    for layer, ruleset in enumerate(rulesets):
        merged.extend(replace(r, layer=layer) for r in ruleset)
    return merged


def evaluate(tool: str, pattern: str, ruleset: list[Rule], *, default: Action = "ask") -> Rule:
    """Pick the winning rule for ``(tool, pattern)``.

    Most specific pattern wins; ties go to the later source (session > project >
    user > builtin), then deny > ask > allow. Falls back to ``{"tool": tool, "pattern": "*", action: default}``.
    User config wins over a project's: a project allow never overrides a matching user (or session) deny, however
    specific it is (a cloned repo's ``git push origin *: allow`` beat the user's ``git push *: deny``).
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
