"""M5 learning: decision log, permission learning, preference distiller, project prep, optimizer."""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "perm_min_approvals": 3,  # same narrow pattern approved this often and never denied -> propose allow
    "perm_min_denials": 2,
    "user_scope_projects": 2,  # a rule seen in this many projects goes to user config
    "rank_threshold": 0.15,  # proposals scoring below this are dropped
    "review_min_turns": 6,  # session length (user turns) that triggers a background review
    "stale_days": 30,
    "auto_gotchas": True,  # a recurring tool failure whose retry worked is learned as a gotcha without a card
    "optimizer": {"enabled": False, "ab_sessions": 20, "min_sessions": 5},
}


def learning_cfg(config: Any) -> dict[str, Any]:
    user = dict(getattr(config, "learning", None) or {})
    out = {**DEFAULTS, **user}
    out["optimizer"] = {**DEFAULTS["optimizer"], **dict(user.get("optimizer") or {})}
    return out
