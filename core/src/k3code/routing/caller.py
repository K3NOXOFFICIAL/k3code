"""Tier-aware one-shot model calls (titles, classification, preview, advisor, judges, …).

Every call names a :class:`TaskKind`; the policy picks the tier, the tier's router walks its
provider chain, and one ``call`` usage row is written with the tier and kind. A ChainExhausted
failure escalates to the next tier (``routing.escalated``) until the top of the ladder.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from k3code.errors import ChainExhausted
from k3code.providers.types import Message, ToolSpec
from k3code.routing.tiers import TaskKind, Tier, TierRouters, next_tier, tier_for

logger = logging.getLogger(__name__)


@dataclass
class CallResult:
    text: str
    tier: Tier
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelCaller:
    """Runs non-agentic completions through the tier routers and accounts for them."""

    def __init__(
        self,
        routers: Callable[[], TierRouters],
        config: Any,
        usage: Any,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        last_attempt: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self._routers = routers
        self.config = config
        self.usage = usage
        self.emit = emit
        self._last_attempt = last_attempt

    async def complete(
        self,
        kind: TaskKind | str,
        messages: list[Message],
        *,
        session_id: str = "",
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2048,
        timeout: float | None = None,
        escalate: bool = True,
        tier: Tier | None = None,
    ) -> CallResult:
        kind = TaskKind(kind)
        routers = self._routers()
        tier = tier or tier_for(kind, getattr(self.config, "task_tiers", None))
        while True:
            try:
                return await self._once(routers, kind, tier, messages, session_id, tools or [], max_tokens, timeout)
            except ChainExhausted as exc:
                nxt = next_tier(tier) if escalate else None
                if nxt is None:
                    raise
                self.note_escalation(kind, tier, nxt, f"chain exhausted: {exc}", session_id)
                tier = nxt

    def note_escalation(self, kind: TaskKind, old: Tier, new: Tier, reason: str, session_id: str = "") -> None:
        logger.info("routing.escalated %s: %s -> %s (%s)", kind.value, old.value, new.value, reason)
        if self.usage is not None:
            self.usage.record("escalated", session=session_id, tier=new.value, task_kind=kind.value,
                              detail=f"{old.value}->{new.value}: {reason}")
        if self.emit is not None:
            self.emit(
                "routing.escalated",
                {"task_kind": kind.value, "from": old.value, "to": new.value, "reason": reason,
                 "session_id": session_id},
            )

    async def _once(
        self,
        routers: TierRouters,
        kind: TaskKind,
        tier: Tier,
        messages: list[Message],
        session_id: str,
        tools: list[ToolSpec],
        max_tokens: int,
        timeout: float | None,
    ) -> CallResult:
        router = routers.get(tier)
        coro = router.complete(messages, tools, max_tokens=max_tokens)
        final = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        usage = final.usage
        provider, model = self._last_attempt() if self._last_attempt else ("", "")
        pt, ct = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        if self.usage is not None:
            self.usage.record(
                "call", session=session_id, provider=provider, model=model, tokens_in=pt, tokens_out=ct,
                cost_usd=usage.cost_usd if usage else None, tier=tier.value, task_kind=kind.value,
            )
        return CallResult(final.content or "", tier, model, provider, pt, ct)
