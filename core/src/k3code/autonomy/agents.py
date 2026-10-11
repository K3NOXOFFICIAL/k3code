"""How many sub-agents a run may start, and how many may run at once: one resolver every caller reads.

Two numbers stay apart. ``max_agents`` is the TOTAL a run (``/ultracode``) may start; ``max_parallel`` is how many
children of a session run AT THE SAME TIME. ``ultracode.max_agents`` and ``autonomy.agents.*`` set them; a session's own
override (``/maxagents``, kept in the session's metadata) replaces them field by field. A value that is no valid number
counts as unset, so a bad hand-edit never breaks a turn (the config load reports it; see :func:`check_settings`).

Dynamic sizing (``autonomy.agents.dynamic``) may only choose a number at or below what these limits allow: it never
spends more than the static configuration does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: ``ultracode.max_agents`` default: children one /ultracode run may start (planners, workers, reviewers, fixer).
DEFAULT_MAX_AGENTS = 12
#: the largest ``ultracode.max_agents`` a config or /maxagents accepts
MAX_AGENTS_CAP = 64
#: ``max_agents: auto`` starts from this many and is trimmed to what the sized plan needs
AUTO_CEILING = 24
#: children running at once for one session: the largest ``autonomy.agents.max_parallel``
HARD_MAX_PARALLEL = 8
#: live sub-agents in the whole process (every session of a daemon) beyond which the model's task calls are refused
DAEMON_LIVE_CEILING = 16
DEFAULT_MAX_PARALLEL = 4
DEFAULT_MAX_NESTED = 3
DEFAULT_MAX_SPAWNS = 16
#: the values of ``max_agents`` that mean "pick it from the task"
AUTO = "auto"

#: the session-override keys ``/maxagents`` sets
OVERRIDE_KEYS = frozenset({"max_agents", "max_parallel", "dynamic"})


@dataclass(frozen=True)
class AgentLimits:
    """The limits in force for one session, and where each came from."""

    #: size the agents from the task (see k3code.autonomy.sizing) instead of one worker per plan step
    dynamic: bool
    #: total children one run may start; None with ``auto`` (the run sizes itself, up to ``AUTO_CEILING``)
    max_agents: int | None
    auto: bool
    #: children of a session running at once (the model's task calls, fan-out width)
    max_parallel: int
    #: live grandchildren one child may have (0 = a child cannot start sub-agents)
    max_nested: int
    #: task calls the model may make in one user turn, per parent (0 = unlimited)
    max_spawns: int
    #: ``autonomy.fanout.max_parallel``: the fan-out's own width ceiling
    fanout_parallel: int
    #: where each value came from: ``session`` | ``config`` | ``default``
    sources: Mapping[str, str]

    @property
    def budget_agents(self) -> int:
        """The agent budget an /ultracode run starts with (``auto`` starts at the ceiling and is trimmed later)."""
        return AUTO_CEILING if self.auto or self.max_agents is None else self.max_agents

    @property
    def fanout_width(self) -> int:
        """The widest a fan-out may run: its own ceiling, bounded by the session's parallel limit."""
        return max(1, min(self.fanout_parallel, self.max_parallel))

    def describe_max_agents(self) -> str:
        return AUTO if self.auto else str(self.max_agents)


def whole_number(value: Any, *, lo: int, hi: int | None = None) -> int | None:
    """``value`` as an int when it is a whole number in range, else None. A float with no fraction counts, and so does
    a string of digits (``max_agents: "12"``, which a hand-written file or an environment variable gives, and which
    ``int()`` has always read); a bool, a fraction, a negative or any other text does not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.isdigit():
            return None
        value = int(text)
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int) or value < lo or (hi is not None and value > hi):
        return None
    return value


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _max_agents(value: Any) -> int | str | None:
    if isinstance(value, str) and value.strip().lower() == AUTO:
        return AUTO
    return whole_number(value, lo=1, hi=MAX_AGENTS_CAP)


def agent_limits(config: Any, override: Mapping[str, Any] | None = None) -> AgentLimits:
    """The limits for a session: ``override`` (its own setting, see /maxagents) beats ``config`` beats the defaults."""
    from k3code.autonomy import autonomy_cfg

    raw = dict(getattr(config, "autonomy", None) or {})
    agents = raw.get("agents") if isinstance(raw.get("agents"), Mapping) else {}  # what the user set, not the defaults
    ultra = dict(getattr(config, "ultracode", None) or {})
    over = dict(override or {})
    sources: dict[str, str] = {}

    def pick(name: str, candidates: list[tuple[str, Any]], default: Any) -> Any:
        for source, value in candidates:
            if value is not None:
                sources[name] = source
                return value
        sources[name] = "default"
        return default

    max_agents = pick(
        "max_agents",
        [("session", _max_agents(over.get("max_agents"))), ("config", _max_agents(ultra.get("max_agents")))],
        DEFAULT_MAX_AGENTS,
    )
    auto = max_agents == AUTO
    fanout_parallel = whole_number(autonomy_cfg(config)["fanout"].get("max_parallel"), lo=1) or 3
    return AgentLimits(
        dynamic=pick(
            "dynamic",
            [("session", _flag(over.get("dynamic"))), ("config", _flag(agents.get("dynamic")))],
            True,
        ),
        max_agents=None if auto else int(max_agents),
        auto=auto,
        max_parallel=pick(
            "max_parallel",
            [
                ("session", whole_number(over.get("max_parallel"), lo=1, hi=HARD_MAX_PARALLEL)),
                ("config", whole_number(agents.get("max_parallel"), lo=1, hi=HARD_MAX_PARALLEL)),
            ],
            DEFAULT_MAX_PARALLEL,
        ),
        max_nested=pick(
            "max_nested",
            [("config", whole_number(agents.get("max_nested"), lo=0, hi=HARD_MAX_PARALLEL))],
            DEFAULT_MAX_NESTED,
        ),
        max_spawns=pick("max_spawns", [("config", whole_number(agents.get("max_spawns"), lo=0))], DEFAULT_MAX_SPAWNS),
        fanout_parallel=min(fanout_parallel, HARD_MAX_PARALLEL),
        sources=sources,
    )


def clean_override(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """The valid part of a stored session override (an imported or hand-edited one may carry anything)."""
    out: dict[str, Any] = {}
    for key, value in dict(raw or {}).items():
        if key == "max_agents" and _max_agents(value) is not None:
            out[key] = _max_agents(value)
        elif key == "max_parallel" and whole_number(value, lo=1, hi=HARD_MAX_PARALLEL) is not None:
            out[key] = int(value)
        elif key == "dynamic" and isinstance(value, bool):
            out[key] = value
    return out


def check_settings(ultracode: Mapping[str, Any], agents: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` for a ``ultracode`` / ``autonomy.agents`` value no run can use (checked when the config
    loads). Only keys that are present are checked, so a config that sets none of them loads as before."""
    if "max_agents" in ultracode and _max_agents(ultracode["max_agents"]) is None:
        raise ValueError(
            f"ultracode.max_agents must be a whole number from 1 to {MAX_AGENTS_CAP}, or {AUTO!r} "
            f"(got {ultracode['max_agents']!r})"
        )
    if "max_tokens" in ultracode and whole_number(ultracode["max_tokens"], lo=1) is None:
        raise ValueError(
            f"ultracode.max_tokens must be a whole number of tokens, 1 or more (got {ultracode['max_tokens']!r})"
        )
    if "dynamic" in agents and _flag(agents["dynamic"]) is None:
        raise ValueError(f"autonomy.agents.dynamic must be true or false (got {agents['dynamic']!r})")
    for key, lo, hi in (
        ("max_parallel", 1, HARD_MAX_PARALLEL),
        ("max_nested", 0, HARD_MAX_PARALLEL),
        ("max_spawns", 0, None),
    ):
        if key in agents and whole_number(agents[key], lo=lo, hi=hi) is None:
            span = f"from {lo} to {hi}" if hi is not None else f"{lo} or more"
            raise ValueError(f"autonomy.agents.{key} must be a whole number {span} (got {agents[key]!r})")


def session_limits(config: Any, session: Any) -> AgentLimits:
    """The agent limits of ``session``: its own /maxagents setting (kept in the session's metadata) over the config."""
    meta = getattr(getattr(session, "stored", None), "meta", None) or {}
    return agent_limits(config, clean_override(meta.get("agent_limits")))
