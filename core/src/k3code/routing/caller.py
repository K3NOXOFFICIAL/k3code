"""Tier-aware one-shot model calls (titles, classification, preview, advisor, judges, …).

Every call names a :class:`TaskKind`; the policy picks the tier, the tier's router walks its
provider chain, and one ``call`` usage row is written with the tier and kind. A ChainExhausted
failure escalates to the next tier (``routing.escalated``) until the top of the ladder.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
        turn_of: Callable[[str], str] | None = None,
    ) -> None:
        self._routers = routers
        self.config = config
        self.usage = usage
        self.emit = emit
        self._last_attempt = last_attempt
        #: session id -> the turn id in flight, so side calls (titles, compaction, judges) join that turn's totals
        self._turn_of = turn_of

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
        router: Any = None,
    ) -> CallResult:
        """``router``: send to this router instead of the tier's (a model named in config); no escalation then."""
        kind = TaskKind(kind)
        routers = self._routers()
        tier = tier or tier_for(kind, getattr(self.config, "task_tiers", None))
        if router is not None:
            return await self._once(routers, kind, tier, messages, session_id, tools or [], max_tokens, timeout, router)
        while True:
            try:
                return await self._once(routers, kind, tier, messages, session_id, tools or [], max_tokens, timeout)
            except ChainExhausted as exc:
                nxt = next_tier(tier) if escalate else None
                if nxt is None:
                    raise
                # every provider of this tier was unreachable: an outage, not a sign the tier is too weak
                self.note_escalation(kind, tier, nxt, f"chain exhausted: {exc}", session_id, cause="outage")
                tier = nxt

    def note_escalation(
        self, kind: TaskKind, old: Tier, new: Tier, reason: str, session_id: str = "", *, cause: str = "quality"
    ) -> None:
        """Record a move up the ladder. ``cause``: "quality" (the attempt stalled; counts toward the optimizer's
        escalation rate) or "outage" (the tier's providers were unreachable; not read as quality)."""
        logger.info("routing.escalated %s: %s -> %s (%s, %s)", kind.value, old.value, new.value, cause, reason)
        if self.usage is not None:
            self.usage.record(
                "outage" if cause == "outage" else "escalated",
                session=session_id,
                tier=new.value,
                task_kind=kind.value,
                detail=f"{old.value}->{new.value}: {reason}",
                turn=self._turn_of(session_id) if self._turn_of else "",
            )
        if self.emit is not None:
            self.emit(
                "routing.escalated",
                {
                    "task_kind": kind.value,
                    "from": old.value,
                    "to": new.value,
                    "reason": reason,
                    "cause": cause,
                    "session_id": session_id,
                },
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
        router: Any = None,
    ) -> CallResult:
        router = router if router is not None else routers.get(tier)
        coro = router.complete(messages, tools, max_tokens=max_tokens)
        started = time.monotonic()
        final = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        seconds = time.monotonic() - started
        usage = final.usage
        provider, model = self._last_attempt() if self._last_attempt else ("", "")
        pt, ct = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        if self.usage is not None:
            self.usage.record(
                "call",
                session=session_id,
                provider=provider,
                model=model,
                tokens_in=pt,
                tokens_out=ct,
                cost_usd=usage.cost_usd if usage else None,
                cache_read=usage.cache_read_tokens if usage else 0,
                cache_write=usage.cache_creation_tokens if usage else 0,
                tier=tier.value,
                task_kind=kind.value,
                turn=self._turn_of(session_id) if self._turn_of else "",
                seconds=seconds,
            )
        return CallResult(final.content or "", tier, model, provider, pt, ct)
