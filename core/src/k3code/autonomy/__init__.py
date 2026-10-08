"""M4a autonomy: scope gate, plan-first, proposals, preview, advisor."""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "plan_first": True,
    #: permission modes in which the scope gate runs ("default" also plans when listed here)
    "gate_modes": ["auto"],
    #: run the plan-first gate for unattended sessions too (cron, loop ticks, automations); off = pre-approved
    "gate_unattended": False,
    #: unattended bash (goal continuations, sub-agents, background runs) keeps the network inside the sandbox;
    #: off = no network (bwrap cannot filter by host, so this is all or nothing)
    "unattended_network": False,
    #: advisor critique after plan approval / before a goal is declared done (auto mode)
    "advisor_on_plan": True,
    "advisor_on_goal": True,
    "advisor_modes": ["auto"],
    #: proposer pass after a plan and after a task
    "proposals": True,
    #: an interactive task the scope gate calls "trivial" starts on the cheap tier and escalates when it stalls
    #: (GOAL B5: route unimportant work to cheaper models, escalating when they struggle)
    "degrade_trivial": True,
    #: failed tool calls in a row before a cheap-tier task escalates. A loop-guard hit always escalates at once (the
    #: attempt stops there), so it has no key; M1 removed the unread ``loop_guard`` and ``judge_not_done`` keys.
    "escalate": {"tool_errors": 3},
    #: name a new session from its first prompt (one cheap-tier call, background); opt-in
    "auto_title": False,
    "preview_timeout": 30,
    "advisor_compact_chars": 24000,
    #: M4b: parallel sub-agent execution of large/huge plans
    #: ``panes``: inside k3 panes, open one read-only pane per child
    "fanout": {
        "enabled": True,
        "panes": False,
        "max_parallel": 3,
        "require_tests": True,
        "test_command": "",
        "test_timeout": 600,
    },
}


#: The keys ``autonomy.escalate`` reads; config load warns about any other key (it would be silently ignored).
ESCALATE_KEYS = frozenset(DEFAULTS["escalate"])


def autonomy_cfg(config: Any) -> dict[str, Any]:
    """Autonomy settings with defaults filled in (``config.autonomy`` overrides)."""
    user = dict(getattr(config, "autonomy", None) or {})
    out = {**DEFAULTS, **user}
    out["escalate"] = {**DEFAULTS["escalate"], **dict(user.get("escalate") or {})}
    out["fanout"] = {**DEFAULTS["fanout"], **dict(user.get("fanout") or {})}
    return out
