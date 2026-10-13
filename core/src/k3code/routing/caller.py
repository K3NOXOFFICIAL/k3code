"""Tier-aware one-shot model calls (titles, classification, preview, advisor, judges, …).

Every call names a :class:`TaskKind`; the policy picks the tier, the tier's router walks its
provider chain, and one ``call`` usage row is written with the tier and kind. A ChainExhausted
failure escalates to the next tier (``routing.escalated``) until the top of the ladder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from k3code.errors import ChainExhausted
from k3code.providers import effort as effort_mod
from k3code.providers.stall import CALL_DEADLINE
from k3code.providers.types import Message, ToolSpec, Usage
from k3code.routing.tiers import TaskKind, Tier, TierRouters, next_tier, tier_for
from k3code.usage import CUT_SHORT

logger = logging.getLogger(__name__)

#: A call with at least this long a deadline (seconds) caps the stall limits that would outlast it (providers/stall.py).
#: Shorter ones (titles, judges: 15-60 s) keep the provider's own limits: a slow first token there is not a stall.
LONG_DEADLINE_S = 75.0

#: (provider, model) of the router attempts of the call in flight in this context, newest last. ``_once`` installs a
#: fresh list around its router call and the gateway's router-event callback appends to it, so a call is priced by
#: what served IT: the one global "last attempt" belongs to whichever call attempted last, and a title on the cheap
#: tier was recorded (and priced) as the strong model another call had just used.
attempt_holder: ContextVar[list[tuple[str, str]] | None] = ContextVar("attempt_holder", default=None)


#: One-shot calls that never get the session's reasoning effort: short mechanical answers on a cheap tier.
NO_EFFORT_KINDS = frozenset(
    {TaskKind.TITLE, TaskKind.COMPACTION, TaskKind.GOAL_JUDGE, TaskKind.PREVIEW, TaskKind.CLASSIFICATION}
    | {TaskKind.CONTEXT_SELECT, TaskKind.LEARNING, TaskKind.PROPOSAL}
)


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

    async def _answer(
        self,
        router: Any,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        timeout: float | None,
        attempts: list[tuple[str, str]],
        book_dead: Callable[[Usage, tuple[str, str]], None],
    ) -> Message:
        """The router's final message. An attempt that died after the provider counted its prompt (``partial_usage``) is
        spend all the same: ``book_dead`` gets it, as the agent loop's ``_metered`` does for a turn.

        A call under a deadline fails over on the first stalled stream: the deadline would cut a second try on the
        same entry short, and the entries after it would never run."""
        stream = getattr(router, "stream", None)
        if stream is None:  # a router that only completes (a test double) has no attempts to meter
            return await router.complete(messages, tools, max_tokens=max_tokens)
        # (no second pass over the entries skipped for a window either: it would run inside the deadline and leave the
        # tier escalation, which gets a fresh deadline, no time)
        under_deadline = {"stall_retries": 0, "retry_deferred": False} if timeout else {}
        events = stream(messages, tools, max_tokens=max_tokens, **under_deadline)
        pending: Usage | None = None
        served = ("", "")  # who the pending prompt was sent to: the attempts list moves on with the next one
        final: Message | None = None
        # a long deadline (compaction) leaves a stall limit of 300 s (a local server) no way to fire first
        # (only with another entry to fail over to: a single entry has nothing to be cut short for)
        long_call = bool(timeout and timeout >= LONG_DEADLINE_S and len(getattr(router, "chain", ())) > 1)
        deadline = CALL_DEADLINE.set(timeout) if long_call else None
        try:
            async for event in events:
                if event.type == "partial_usage" and event.usage is not None:
                    if pending is not None:  # an earlier attempt died before a retry's prompt was counted
                        book_dead(pending, served)
                    pending, served = event.usage, (attempts[-1] if attempts else ("", event.usage.model))
                elif event.type == "reset":
                    if pending is not None:
                        book_dead(pending, served)
                        pending = None
                elif event.type == "done" and event.message is not None:
                    pending, final = None, event.message  # the final usage replaces the prompt count
        finally:
            if deadline is not None:
                CALL_DEADLINE.reset(deadline)
            if pending is not None:  # the walk ended in an error, or the deadline cancelled it
                book_dead(pending, served)
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        if final is None:
            raise ChainExhausted("provider stream ended without a final message", last_reason="unknown")
        return final

    def _book(
        self,
        usage: Usage | None,
        served: tuple[str, str],
        session_id: str,
        tier: Tier,
        kind: TaskKind,
        *,
        seconds: float = 0.0,
        detail: str = "",
    ) -> None:
        """Charge a call's usage to the session and write its row."""
        provider, model = served
        pt, ct = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        # charged before the row is written: the day's ledger, started from the log, must not read this call twice
        if self._charge is not None and usage is not None and session_id:
            try:
                self._charge(session_id, usage)
            except Exception:  # noqa: BLE001 - accounting must never fail the call it accounts for
                logger.debug("charging a side call failed", exc_info=True)
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
                detail=detail,
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
        *,
        retried: bool = False,
    ) -> CallResult:
        router = router if router is not None else routers.get(tier)
        started = time.monotonic()
        attempts: list[tuple[str, str]] = []
        token = attempt_holder.set(attempts)
        # the session's /effort is for the work it asked for: a title, a verdict or a summary at "max" spent the reply
        # cap on reasoning and ended empty, at several times the cost
        effort_token = effort_mod.REASONING_EFFORT.set(None) if kind in NO_EFFORT_KINDS else None
        try:
            coro = self._answer(
                router,
                messages,
                tools,
                max_tokens,
                timeout,
                attempts,
                lambda usage, served: self._book(usage, served, session_id, tier, kind, detail=CUT_SHORT),
            )
            final = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        finally:
            attempt_holder.reset(token)
            if effort_token is not None:
                effort_mod.REASONING_EFFORT.reset(effort_token)
        seconds = time.monotonic() - started
        usage = final.usage
        provider, model = self._served(attempts, usage)
        pt, ct = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        self._book(usage, (provider, model), session_id, tier, kind, seconds=seconds)
        result = CallResult(final.content or "", tier, model, provider, pt, ct)
        if (
            not result.text.strip()
            and not retried
            and kind not in NO_EFFORT_KINDS
            and effort_mod.REASONING_EFFORT.get()
            and self._effort_reached(provider, model)
        ):
            # a thinking model spent the whole cap on reasoning (a 500-token verdict at /effort max) and wrote nothing:
            # the call above is billed and logged; ask once more without the effort rather than return an empty answer
            quiet = effort_mod.REASONING_EFFORT.set(None)
            try:
                again = await self._once(
                    routers, kind, tier, messages, session_id, tools, max_tokens, timeout, router, retried=True
                )
            except Exception:  # noqa: BLE001 - the first answer was valid, if empty: the retry must not replace it
                logger.debug("the retry without the effort failed; keeping the first answer", exc_info=True)
                return result
            finally:
                effort_mod.REASONING_EFFORT.reset(quiet)
            # both calls are billed: the caller adds these to its own cap
            again.prompt_tokens += pt
            again.completion_tokens += ct
            return again
        return result

    def _effort_reached(self, provider: str, model: str) -> bool:
        """Whether the session's effort was part of the request the served model got: a retry without it is a different
        request only then. What cannot be judged (no model id, an unknown provider kind) counts as yes."""
        kind = next((p.kind for p in getattr(self.config, "providers", None) or () if p.name == provider), "")
        if kind == "claude-cli":  # passes --effort to whatever model it runs
            return True
        # a provider no router event named is judged by the model id alone, under either kind
        kinds = (kind,) if kind else ("anthropic", "openai")
        return any(effort_mod.takes_effort(model, k) is not False for k in kinds)
