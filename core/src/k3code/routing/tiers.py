"""Model tiers, the task-kind → tier policy table, escalation and per-tier routers.

Tiers: ``main`` (default coding), ``strong`` (planning/review/advisor), ``cheap``
(summaries, titles, judges, compaction, classification), ``fast`` (previews).
Each provider block may list models per tier (``tiers: {strong: [...]}``); a tier
without a list falls back to ``models[<tier>]`` and then ``models.default``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from k3code.router import CooldownStore, Router, RouterEvent, build_chain

logger = logging.getLogger(__name__)


class Tier(StrEnum):
    MAIN = "main"
    STRONG = "strong"
    CHEAP = "cheap"
    FAST = "fast"


class TaskKind(StrEnum):
    INTERACTIVE_TURN = "interactive_turn"
    BACKGROUND_TURN = "background_turn"
    SUBAGENT = "subagent"
    TITLE = "title"
    COMPACTION = "compaction"
    GOAL_JUDGE = "goal_judge"
    LOOP_TICK = "loop_tick"
    CRON_JOB = "cron_job"
    REVIEW = "review"
    PLAN = "plan"
    PREVIEW = "preview"
    ADVISOR = "advisor"
    RESEARCH_SEARCH = "research_search"
    CLASSIFICATION = "classification"


DEFAULT_POLICY: dict[TaskKind, Tier] = {
    TaskKind.INTERACTIVE_TURN: Tier.MAIN,
    TaskKind.BACKGROUND_TURN: Tier.CHEAP,
    TaskKind.SUBAGENT: Tier.MAIN,
    TaskKind.TITLE: Tier.CHEAP,
    TaskKind.COMPACTION: Tier.CHEAP,
    TaskKind.GOAL_JUDGE: Tier.CHEAP,
    TaskKind.LOOP_TICK: Tier.CHEAP,
    TaskKind.CRON_JOB: Tier.CHEAP,
    TaskKind.REVIEW: Tier.STRONG,
    TaskKind.PLAN: Tier.STRONG,
    TaskKind.PREVIEW: Tier.FAST,
    TaskKind.ADVISOR: Tier.STRONG,
    TaskKind.RESEARCH_SEARCH: Tier.CHEAP,
    TaskKind.CLASSIFICATION: Tier.CHEAP,
}

#: Escalation ladder: cheaper tiers climb towards ``strong``.
LADDER: tuple[Tier, ...] = (Tier.FAST, Tier.CHEAP, Tier.MAIN, Tier.STRONG)


def tier_for(kind: TaskKind | str, overrides: dict[str, str] | None = None) -> Tier:
    """The tier for a task kind; ``overrides`` (config ``task_tiers``) win over the defaults."""
    kind = TaskKind(kind)
    if overrides and kind.value in overrides:
        try:
            return Tier(overrides[kind.value])
        except ValueError:
            logger.warning("task_tiers: unknown tier %r for %s; using the default", overrides[kind.value], kind.value)
    return DEFAULT_POLICY[kind]


def next_tier(tier: Tier | str) -> Tier | None:
    """The next tier up the ladder, or None at the top."""
    tier = Tier(tier)
    i = LADDER.index(tier)
    return LADDER[i + 1] if i + 1 < len(LADDER) else None


def tier_model_specs(config: Any, tier: Tier | str, *, key: str | None = None) -> list[str | list[str]]:
    """Per-provider model specs for a tier.

    ``main`` honours the configured default model key (as before M4a), so a config without
    any ``tiers`` behaves exactly as it did. Other tiers: ``tiers[tier]`` → ``models[tier]``
    → ``models.default`` → first model.
    """
    tier = Tier(tier)
    resolved: list[str | list[str]] = []
    for p in config.providers:
        own = (getattr(p, "tiers", {}) or {}).get(tier.value)
        if tier is Tier.MAIN:
            k = key or config.default_model
            # an explicit /model key beats tiers.main; the plain "default" key defers to it
            spec = (own or p.models.get(k)) if k == "default" else (p.models.get(k) or own)
        else:
            spec = own or p.models.get(tier.value)
        if spec is None:
            spec = p.models.get("default")
        if spec is None:
            spec = next(iter(p.models.values()), "")
        resolved.append(spec)
    return resolved


def router_options(config: Any) -> dict[str, float]:
    """``router:`` settings (max_inline_wait, quota_cooldown) as Router keyword arguments."""
    raw = getattr(config, "router", None) or {}
    return {k: float(raw[k]) for k in ("max_inline_wait", "quota_cooldown") if k in raw}


class TierRouters:
    """One :class:`Router` per tier over shared providers and cooldowns, built lazily."""

    def __init__(
        self,
        providers: list[Any],
        config: Any,
        *,
        cooldowns: CooldownStore | None = None,
        on_event: Callable[[RouterEvent], None] | None = None,
        main_key: str | None = None,
        fallback_router: Router | None = None,
    ) -> None:
        #: when set, every tier uses this router (a single pre-built chain)
        self.fallback_router = fallback_router
        self.providers = providers
        self.config = config
        self.cooldowns = cooldowns if cooldowns is not None else CooldownStore()
        self.on_event = on_event
        self.main_key = main_key
        self._routers: dict[Tier, Router] = {}

    def get(self, tier: Tier | str) -> Router:
        tier = Tier(tier)
        if self.fallback_router is not None:
            return self.fallback_router
        if tier not in self._routers:
            chain = build_chain(self.providers, tier_model_specs(self.config, tier, key=self.main_key))
            self._routers[tier] = Router(
                chain, cooldowns=self.cooldowns, on_event=self.on_event, tier=tier.value, **router_options(self.config)
            )
        return self._routers[tier]

    def for_kind(self, kind: TaskKind | str) -> Router:
        return self.get(tier_for(kind, getattr(self.config, "task_tiers", None)))


class Escalation:
    """Tracks failures of a task on a tier; after ``threshold`` of them moves to the next tier.

    Failure signals: ``tool_errors`` (repeated tool errors) and ``loop_guard`` (the loop guard fired). Each
    ``record`` counts one failure; ``record`` returns the new tier when it escalates, else None.
    """

    #: failures of a signal that trigger escalation
    DEFAULT_THRESHOLDS = {"tool_errors": 3, "loop_guard": 1}

    def __init__(
        self,
        tier: Tier | str,
        *,
        thresholds: dict[str, int] | None = None,
        on_escalate: Callable[[Tier, Tier, str], None] | None = None,
    ) -> None:
        self.tier = Tier(tier)
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.counts: dict[str, int] = {}
        self.on_escalate = on_escalate
        self.history: list[tuple[Tier, Tier, str]] = []

    def record(self, signal: str) -> Tier | None:
        self.counts[signal] = self.counts.get(signal, 0) + 1
        if self.counts[signal] < self.thresholds.get(signal, 1):
            return None
        return self.escalate(signal)

    def escalate(self, reason: str) -> Tier | None:
        nxt = next_tier(self.tier)
        if nxt is None:
            return None
        old, self.tier = self.tier, nxt
        self.counts = {}
        self.history.append((old, nxt, reason))
        logger.info("routing.escalated %s -> %s (%s)", old.value, nxt.value, reason)
        if self.on_escalate is not None:
            self.on_escalate(old, nxt, reason)
        return nxt
