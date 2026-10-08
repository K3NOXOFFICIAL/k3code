# Vendored rule engine: opencode-style permissions, ported to Python.
# Sources: anomalyco/opencode@ecc4916b5a9608c30e6dd58a67f2137b594407ca
#   packages/opencode/src/util/wildcard.ts (MIT)
#   packages/opencode/src/permission/arity.ts (MIT)
#   packages/opencode/src/permission/index.ts (MIT)

"""Permission engine: modes, rules, hardline denies.

Rules look like ``{"tool": "bash", "pattern": "git status*", "action": "allow"}``.
``action`` is one of ``allow`` | ``ask`` | ``deny``. Winner among matching
rules: most specific pattern first, then deny > ask > allow, then later
rulesets (session > project > user > builtin).
"""

from __future__ import annotations

from . import arity, hardline, rules, wildcard
from .engine import (
    BUILTIN_BASH_ALLOW,
    EDIT_TOOLS,
    EXIT_PLAN_TOOL,
    PURE_TOOLS,
    READ_TOOLS,
    Decision,
    InvalidPermissionMode,
    PermissionMode,
    builtin_defaults,
    decide,
    permission_mode_from_config,
    suggest_rules,
)
from .rules import Action, Rule
from .rules import evaluate as evaluate
from .rules import from_config as from_config
from .rules import merge as merge

MODE_CYCLE_NAMES = ["default", "accept-edits", "plan", "auto"]

__all__ = [
    "MODE_CYCLE_NAMES",
    "Action",
    "BUILTIN_BASH_ALLOW",
    "Decision",
    "EDIT_TOOLS",
    "EXIT_PLAN_TOOL",
    "PURE_TOOLS",
    "READ_TOOLS",
    "InvalidPermissionMode",
    "PermissionMode",
    "Rule",
    "permission_mode_from_config",
    "arity",
    "builtin_defaults",
    "check_permission",
    "decide",
    "suggest_rules",
    "from_config",
    "hardline",
    "merge",
    "rules",
    "wildcard",
]


def check_permission(
    mode: PermissionMode | str,
    tool_name: str,
    *,
    headless: bool = False,
) -> tuple[bool, str | None]:
    """Compat wrapper over :func:`decide` (M0 signature, no args available).

    Only consults the mode; tools that need argument-aware checks (bash
    command patterns, paths) must go through :func:`decide`.
    """
    decision = decide(mode=PermissionMode(mode), tool=tool_name, headless=headless)
    return decision.action == "allow", decision.message
