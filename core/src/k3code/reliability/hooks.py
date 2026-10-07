"""Reliability hooks: glue between the agent loop and the reliability layer.

The loop holds one :class:`Reliability` bundle; every hook is off when the
bundle says so, cheap (no disk, no extra network), and marked in the loop code
with an ``M2:`` comment. NetWatch polling is only started when the entry point
opted in (``reliability.start()`` in the CLI); everything else works without it.

Events are forwarded through the same ``on_event`` callback the loop already
has, using the ``reliability.*`` / ``net.state`` kinds from
:mod:`k3code.reliability.events`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec
from k3code.reliability import events as ev
from k3code.reliability.events import EventEmitter
from k3code.reliability.governor import Budget, BudgetExceeded, DiskGuardFull, Governor, GovernorConfig
from k3code.reliability.journal import ToolJournal, interrupted_result
from k3code.reliability.loopguard import GuardOutcome, LoopGuard, Verdict
from k3code.reliability.netwatch import NetState, NetWatch, NetWatchConfig
from k3code.reliability.persistent_retry import CancelToken, PersistentRetry, RetryConfig

logger = logging.getLogger(__name__)


@dataclass
class ReliabilityFlags:
    """Per-hook switches; all on by default when a Reliability bundle is built."""

    netwatch: bool = True
    persistent_retry: bool = True
    journal: bool = True
    loop_guard: bool = True
    budget_guard: bool = True


@dataclass
class ReliabilitySettings:
    """Config-file shape for the reliability layer."""

    enabled: bool = True
    flags: ReliabilityFlags = field(default_factory=ReliabilityFlags)
    max_wait: float | None = None  # persistent-retry total wait bound; None = forever
    max_park_seconds: float = 600.0  # park ladder cap mirror (informational; RetryConfig owns it)
    # Optional session budget caps, enforced from router usage reports.
    session_tokens: int | None = None
    session_usd: float | None = None
    day_tokens: int | None = None
    day_usd: float | None = None
    # Raw NetWatchConfig overrides (e.g. http_probe_url/tcp_probe_host/tcp_probe_port)
    # so chaos tooling can point the generic internet probe at a local proxy
    # instead of the real internet. Empty means NetWatchConfig defaults.
    netwatch: dict[str, Any] = field(default_factory=dict)


class Reliability:
    """One bundle of all reliability hooks for an agent loop.

    Construct with :meth:`disabled` for unit tests, or :meth:`from_settings`
    in the CLI/REPL entry points. :meth:`start` begins NetWatch polling (with
    provider endpoints registered from the router chain); :meth:`stop` ends it.
    """

    def __init__(
        self,
        *,
        flags: ReliabilityFlags | None = None,
        session: str = "default",
        home: Path | None = None,
        cancel_token: CancelToken | None = None,
        max_wait: float | None = None,
        netwatch_config: dict[str, Any] | None = None,
    ) -> None:
        self.flags = flags or ReliabilityFlags()
        self.session = session
        self.home = home
        self.events = EventEmitter()
        self.cancel_token = cancel_token or CancelToken()
        self.netwatch = NetWatch(NetWatchConfig(**(netwatch_config or {}))) if self.flags.netwatch else None
        self.retry_config = RetryConfig(max_wait=max_wait)
        self.governor = Governor(GovernorConfig(), events=self.events) if self.flags.budget_guard else None
        self.loop_guard = LoopGuard() if self.flags.loop_guard else None
        self.journal: ToolJournal | None = None  # opened lazily in _open_journal
        self.retry: PersistentRetry | None = None  # built in attach_router
        self._started = False

    # ── construction ──

    @classmethod
    def disabled(cls) -> Reliability:
        """All hooks off (unit-test seam)."""
        return cls(flags=ReliabilityFlags(False, False, False, False, False))

    @classmethod
    def from_settings(
        cls,
        settings: ReliabilitySettings | None,
        *,
        session: str = "default",
        home: Path | None = None,
    ) -> Reliability:
        """Build from config; settings=None means "reliability on, all defaults"."""
        if settings is not None and not settings.enabled:
            return cls.disabled()
        r = cls(
            flags=settings.flags if settings else ReliabilityFlags(),
            session=session,
            home=home,
            max_wait=settings.max_wait if settings else None,
            netwatch_config=settings.netwatch if settings else None,
        )
        if settings and r.governor is not None:
            if settings.session_tokens is not None or settings.session_usd is not None:
                r.governor.add_budget(
                    Budget(scope="session", tokens=settings.session_tokens, usd=settings.session_usd)
                )
            if settings.day_tokens is not None or settings.day_usd is not None:
                r.governor.add_budget(Budget(scope="day", tokens=settings.day_tokens, usd=settings.day_usd))
        return r

    # ── lifecycle ──

    def attach_router(self, router: Any) -> None:
        """Build the persistent-retry wrapper around the live router."""
        if self.flags.persistent_retry:
            self.retry = PersistentRetry(
                router,
                self.netwatch,
                config=self.retry_config,
                events=self.events,
                cancel_token=self.cancel_token,
            )
            self.retry_config = self.retry.config

    async def start(self) -> None:
        """Begin NetWatch polling (providers should already be registered)."""
        if self.netwatch is not None and not self._started:
            self._forward_net_states()
            await self.netwatch.start()
            self._started = True

    async def stop(self) -> None:
        if self.netwatch is not None and self._started:
            await self.netwatch.stop()
            self._started = False
        if self.journal is not None:
            self.journal.close()
            self.journal = None

    def register_providers(self, chain: Any) -> None:
        """Register each router chain entry's base URL with NetWatch."""
        if self.netwatch is None:
            return
        for entry in chain or []:
            name = getattr(entry, "provider_name", None) or getattr(entry, "name", None)
            base_url = getattr(entry, "base_url", None)
            if name and base_url:
                self.netwatch.add_provider(str(name), str(base_url))

    # ── stream wrapper (M2: persistent retry) ──

    def stream(
        self,
        router: Any,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Route the stream through PersistentRetry when enabled, else straight through."""
        if self.flags.persistent_retry and self.retry is not None:
            return self.retry.stream(messages, tools, model=model, max_tokens=max_tokens, temperature=temperature)
        return router.stream(messages, tools, model=model, max_tokens=max_tokens, temperature=temperature)

    # ── tool hooks (M2: journal + loop guard) ──

    def observe_tool_request(self, call: ToolCall) -> GuardOutcome | None:
        """Loop-guard check for a pending tool call (None when the guard is off)."""
        if self.loop_guard is None:
            return None
        outcome = self.loop_guard.observe_tool_call(call.name, call.arguments)
        self._emit_guard(outcome)
        return outcome

    def observe_assistant(self, content: str | None) -> GuardOutcome | None:
        """Loop-guard check for an assistant message without tool calls."""
        if self.loop_guard is None:
            return None
        outcome = self.loop_guard.observe_message(content)
        self._emit_guard(outcome)
        return outcome

    def _emit_guard(self, outcome: GuardOutcome) -> None:
        if outcome.verdict is Verdict.NOTE:
            self.events.emit(ev.LOOP_NOTE_INJECTED, detail=outcome.key)
        elif outcome.verdict is Verdict.STOP:
            self.events.emit(ev.LOOP_DETECTED, detail=outcome.key)
            self.events.emit(ev.NEEDS_INPUT, detail="loop guard stopped the turn")

    def journal_intent(self, call: ToolCall, *, side_effect: bool) -> None:
        """M2: fsync an intent record before the tool runs."""
        if not self.flags.journal:
            return
        journal = self._open_journal()
        if journal is not None:
            journal.record_intent(call.id, call.name, call.arguments, side_effect)

    def journal_done(self, call_id: str, result: Any) -> None:
        """M2: record a completion digest after the tool runs."""
        if self.journal is not None:
            self.journal.record_done(call_id, result)

    # ── transcript persistence (resume after a crash) ──

    def _transcript_path(self) -> Path | None:
        if self.home is None:
            return None
        return self.home / "journal" / f"{self.session}.messages.json"

    def save_transcript(self, messages: list[Message]) -> None:
        """M2: atomically persist the conversation so a crashed run can resume."""
        path = self._transcript_path()
        if path is None or not self.flags.journal:
            return
        data = [
            {
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "name": m.name,
                "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls],
            }
            for m in messages
        ]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)
        except OSError as e:
            logger.debug("transcript not saved (%s)", e)

    def load_transcript(self) -> list[Message] | None:
        path = self._transcript_path()
        if path is None or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            return [
                Message(
                    role=d["role"],
                    content=d.get("content"),
                    tool_call_id=d.get("tool_call_id"),
                    name=d.get("name"),
                    tool_calls=[
                        ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
                        for c in d.get("tool_calls", [])
                    ],
                )
                for d in raw
            ]
        except (OSError, ValueError, KeyError):
            return None

    def interrupted_for(self, call: ToolCall) -> dict[str, Any] | None:
        """INTERRUPTED result if this call has an intent without done and is side-effecting."""
        journal = self._open_journal()
        if journal is None:
            return None
        for pending in journal.resume_plan():
            if pending.id == call.id and pending.side_effect:
                self.events.emit(ev.INTERRUPTED_TOOL, detail=f"{pending.tool} ({pending.id})")
                return interrupted_result(pending.tool)
        return None

    def _open_journal(self) -> ToolJournal | None:
        if self.journal is None and self.home is not None:
            try:
                self.journal = ToolJournal(self.home, self.session)
            except OSError as e:
                logger.debug("journal unavailable (%s); continuing without it", e)
                return None
        return self.journal

    # ── budget hooks (M2: governor) ──

    def record_usage(self, usage: Any) -> None:
        """Apply a usage report (done event → usage → governor)."""
        if self.governor is None or usage is None:
            return
        try:
            p = int(getattr(usage, "prompt_tokens", 0) or 0)
            c = int(getattr(usage, "completion_tokens", 0) or 0)
            self.governor.record_usage(p, c)
        except (TypeError, ValueError):
            logger.debug("ignoring malformed usage report: %r", usage)

    def check_budgets(self) -> BudgetExceeded | None:
        """M2: report the first violated budget (emits reliability.budget_exceeded)."""
        if self.governor is None:
            return None
        err = self.governor.check_budgets()
        if err is not None:
            self.events.emit(ev.BUDGET_EXCEEDED, detail=str(err), scope=err.scope, kind=err.kind)
        return err

    def check_disk(self) -> DiskGuardFull | None:
        """M2: report the disk guard trip before starting new work."""
        if self.governor is None:
            return None
        return self.governor.disk_stop()

    # ── resume ──

    def pending_tool_results(self) -> list[tuple[ToolCall, dict[str, Any]]]:
        """M2: crash-resume plan — pure intents for re-run, INTERRUPTED for side effects.

        Returns (call, synthesized-or-empty) pairs; the caller re-runs pure
        intents verbatim and injects the synthesized INTERRUPTED result for
        side-effectful ones (never re-runs them).
        """
        journal = self._open_journal()
        if journal is None:
            return []
        out: list[tuple[ToolCall, dict[str, Any]]] = []
        for pending in journal.resume_plan():
            call = ToolCall(id=pending.id, name=pending.tool, arguments=pending.args)
            if pending.side_effect:
                self.events.emit(ev.INTERRUPTED_TOOL, detail=f"{pending.tool} ({pending.id})")
                res: dict[str, Any] = interrupted_result(pending.tool)
            else:
                res = {}
            out.append((call, res))
        return out

    # ── internals ──

    def _forward_net_states(self) -> None:
        assert self.netwatch is not None

        def on_change(old: NetState, new: NetState) -> None:
            self.events.emit(ev.NET_STATE, detail=f"{old.value} -> {new.value}", old=old.value, new=new.value)

        self.netwatch.subscribe(on_change)

    def cancel(self) -> None:
        """Abort any paused/parked wait (wired to /stop)."""
        self.cancel_token.cancel()

    @property
    def needs_input(self) -> bool:
        """True when the loop guard stopped a turn waiting for user input."""
        return self.loop_guard is not None and self.loop_guard.needs_input


def build_reliability(config: Any, session: str, home: Path | None = None) -> Reliability:
    """Build the reliability bundle from ``config.reliability`` (a dict shaped like ReliabilitySettings)."""
    raw = dict(getattr(config, "reliability", None) or {})
    flags_raw = raw.pop("flags", None)
    flags = ReliabilityFlags(**flags_raw) if isinstance(flags_raw, dict) else None
    known = {"enabled", "max_wait", "max_park_seconds", "session_tokens", "session_usd", "day_tokens",
             "day_usd", "netwatch"}
    settings = ReliabilitySettings(flags=flags) if flags else ReliabilitySettings()
    for key in known & set(raw):
        setattr(settings, key, raw[key])
    return Reliability.from_settings(settings, session=session, home=home)
