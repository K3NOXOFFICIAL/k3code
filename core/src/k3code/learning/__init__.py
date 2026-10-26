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
    "skill_cards_per_week": 3,  # new skill-save cards a project gets per week from session reviews
    "auto_gotchas": True,  # a recurring tool failure whose retry worked is learned as a gotcha without a card
    # enabled: gate-approved candidates are applied by themselves as A/B experiments; off, they are only proposed
    # (the weekly analysis runs either way). experiment_max_days: an experiment still active after that long is rolled
    # back as expired. prices: {model: {"in": USD per 1M input tokens, "out": ..., "cache_read": ...}} so the A/B can
    # judge cost in dollars (a model without a price keeps the comparison in tokens)
    "optimizer": {"enabled": False, "ab_sessions": 20, "min_sessions": 5, "experiment_max_days": 14, "prices": {}},
}


def learning_cfg(config: Any) -> dict[str, Any]:
    user = dict(getattr(config, "learning", None) or {})
    out = {**DEFAULTS, **user}
    out["optimizer"] = {**DEFAULTS["optimizer"], **dict(user.get("optimizer") or {})}
    return out
