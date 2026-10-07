"""M4a autonomy: scope gate, plan-first, proposals, preview, advisor."""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "plan_first": True,
    #: permission modes in which the scope gate runs ("default" also plans when listed here)
    "gate_modes": ["auto"],
    #: advisor critique after plan approval / before a goal is declared done (auto mode)
    "advisor_on_plan": True,
    "advisor_on_goal": True,
    "advisor_modes": ["auto"],
    #: proposer pass after a plan and after a task
    "proposals": True,
    #: failures per signal before a cheap-tier task escalates
    "escalate": {"tool_errors": 3, "loop_guard": 1, "judge_not_done": 2},
    "preview_timeout": 30,
    "advisor_compact_chars": 24000,
}


def autonomy_cfg(config: Any) -> dict[str, Any]:
    """Autonomy settings with defaults filled in (``config.autonomy`` overrides)."""
    user = dict(getattr(config, "autonomy", None) or {})
    out = {**DEFAULTS, **user}
    out["escalate"] = {**DEFAULTS["escalate"], **dict(user.get("escalate") or {})}
    return out
