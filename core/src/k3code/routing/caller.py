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
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from k3code.errors import ChainExhausted
from k3code.providers.types import Message, ToolSpec
from k3code.routing.tiers import TaskKind, Tier, TierRouters, next_tier, tier_for

logger = logging.getLogger(__name__)

#: (provider, model) of the router attempts of the call in flight in this context, newest last. ``_once`` installs a
#: fresh list around its router call and the gateway's router-event callback appends to it, so a call is priced by
#: what served IT: the one global "last attempt" belongs to whichever call attempted last, and a title on the cheap
#: tier was recorded (and priced) as the strong model another call had just used.
attempt_holder: ContextVar[list[tuple[str, str]] | None] = ContextVar("attempt_holder", default=None)


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
        charge: Callable[[str, Any], None] | None = None,
    ) -> None:
        self._routers = routers
        self.config = config
        self.usage = usage
        self.emit = emit
        self._last_attempt = last_attempt
        #: session id -> the turn id in flight, so side calls (titles, compaction, judges) join that turn's totals
        self._turn_of = turn_of
        #: (session id, usage) → applies a call's usage to that session's budgets: a side call (compaction, scope
        #: classifier, goal judge, advisor) is spend like any other, and was written to the usage log only
        self._charge = charge

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

    def _served(self, attempts: list[tuple[str, str]], usage: Any) -> tuple[str, str]:
        """(provider, model) that answered THIS call: its own last router attempt, else the model the router tagged on
        its usage. The global last attempt is the last resort (a router that reports nothing)."""
        if attempts:
            return attempts[-1]
        if usage is not None and usage.model:
            return "", usage.model
        return self._last_attempt() if self._last_attempt else ("", "")

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
        attempts: list[tuple[str, str]] = []
        token = attempt_holder.set(attempts)
        try:
            final = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        finally:
            attempt_holder.reset(token)
        seconds = time.monotonic() - started
        usage = final.usage
        provider, model = self._served(attempts, usage)
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
        if self._charge is not None and usage is not None and session_id:
            try:
                self._charge(session_id, usage)
            except Exception:  # noqa: BLE001 - accounting must never fail the call it accounts for
                logger.debug("charging a side call failed", exc_info=True)
        return CallResult(final.content or "", tier, model, provider, pt, ct)
