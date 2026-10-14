"""Asyncio JSON-RPC 2.0 stdio gateway: the M1-core server the vendored TUI talks to.

Implements the 33 M1-core methods, emits the 24 M1-core events and issues the
4 M1-core server→client requests (clarify, approval, sudo, secret) per
``docs/tui-contract.md``. One live session is supported for M1 (the TUI's
default flow); the session store persists to ``$K3CODE_HOME/sessions.db``.

Wire protocol (``gateway/protocol.py``):
- client→server request:  ``{"jsonrpc":"2.0","id":..,"method":..,"params":{..}}``
- client→server notification: same without ``id`` (no response)
- server→client response / error frames
- server→client event:    ``{"method":"event","params":{"type":..,"payload":{..}}}``
- server→client request:  ``{"id":"k3-N","method":"approval"|"clarify"|..}``

All logging goes to stderr; stdout carries only JSON-RPC frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from k3code import confio, mcpjson, userhooks, wakewords
from k3code import skills as skills_mod
from k3code._version import __version__
from k3code.agent.loop import AgentLoop, ApprovalResult
from k3code.artifacts import ArtifactStore
from k3code.autonomy import advisor, autonomy_cfg
from k3code.autonomy.agents import agent_limits, clean_override, session_limits
from k3code.autonomy.fanout import FanoutExecutor, dirty_checkout, dirty_text
from k3code.autonomy.plan_first import GateResult, PlanFirst
from k3code.autonomy.scope import SCOPES
from k3code.autonomy.ultra import Ultra, ultra_cfg
from k3code.blockers import BlockerStore
from k3code.commands import CommandRegistry
from k3code.commands import tune as tune_cmd
from k3code.commands.builtin import build_registry as build_commands
from k3code.commands.ultra_cmd import JobSpec, typed_mode_word
from k3code.config import Settings, default_project_dir, load_config, retention
from k3code.context_budget import (
    COMPACT_AT_RATIO,
    ELIDE_MIN_CHARS,
    POINTER_CALL_RE,
    SHRINK_FLOOR_CHARS,
    SHRINK_SHARE,
    AutoCompact,
    autocompact_policy,
    compact_resume_at,
    compact_threshold,
    context_window,
    cut_call_text,
    elide_marker,
    overhead_tokens,
    result_chars,
    router_window,
    text_tokens,
    tool_output_limit,
)
from k3code.context_select import decide, decision_settings
from k3code.errors import AllProvidersUnreachable, ChainExhausted, ContextOverflow
from k3code.extratools import register_mcp_tools, register_skill_tool
from k3code.gateway import auth as gw_auth
from k3code.gateway import tui_display
from k3code.gateway.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TOO_MANY_REQUESTS,
    UNAUTHORIZED,
    decode_frame,
    encode_error,
    encode_event,
    encode_response,
    encode_server_request,
    next_request_id,
    reply_id,
)
from k3code.gateway.sessions import (
    EMPTY_SESSION_MAX_AGE_S,
    SessionStore,
    SessionSummary,
    scrub_surrogates,
)
from k3code.goals import (
    CONTINUATION_PROMPT_RECOVER_TEMPLATE,
    GOAL_QUOTE_CHARS,
    MAX_KICKS_PER_WINDOW,
    MAX_LOOP_RECOVERIES,
    GoalManager,
    make_judge,
    quote,
)
from k3code.halt import Halt, clear_halt, load_halt, set_halt
from k3code.learning.hub import LearningHub
from k3code.mcpclient import McpManager
from k3code.memory import MemoryFile, fenced, read_learned
from k3code.paths import project_config_path as _proj_cfg
from k3code.paths import user_config_path as _user_cfg
from k3code.permissions import MODE_CYCLE_NAMES, PermissionMode, permission_mode_from_config, suggest_rules
from k3code.permissions.state import PermissionState, persist_rules, project_config_path
from k3code.pricing import day_loader, estimate_usd
from k3code.prompting import build_system_prompt
from k3code.providers import effort as effort_mod
from k3code.providers import make_providers, provider_signature
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.redact import redact, scrub_text
from k3code.reliability import BudgetExceeded, DiskGuardFull, Reliability, build_reliability
from k3code.reliability import events as rev
from k3code.reliability.governor import DAY_LEDGER
from k3code.reliability.journal import delete_session_journal, prune_journals
from k3code.reliability.persistent_retry import PERMANENT_REASONS, TurnCancelled
from k3code.research.browser import BrowserManager
from k3code.research.fetch import WebFetcher
from k3code.research.flow import Research
from k3code.research.tools import register_web_tools
from k3code.router import CooldownStore, Router, RouterEvent, build_chain
from k3code.routing.caller import ModelCaller, attempt_holder
from k3code.routing.tiers import (
    Escalation,
    TaskKind,
    Tier,
    TierRouters,
    next_tier,
    router_options,
    tier_for,
    tier_model_specs,
)
from k3code.session_ai import (
    SUMMARY_PREFIX,
    compact_messages,
    is_shell_record,
    make_title,
    shell_record,
    shell_record_parts,
)
from k3code.subagents import SubagentManager
from k3code.subagents.tools import register_task_tools
from k3code.tools import MAX_TOOL_RESULT_CHARS, FileStamps, clip_for_model, register_todo, render_todos, shrink_result
from k3code.tools import build_registry as build_tool_registry
from k3code.tools import jobs as tool_jobs
from k3code.usage import UsageDB

logger = logging.getLogger("k3code.gateway")

#: Longest JSON-RPC line read from a client (a pasted prompt can be megabytes); asyncio's default is 64 KiB. A longer
#: one is answered with an error and skipped (it was 64 MiB: a few such frames, parsed twice each, held GBs).
MAX_FRAME_BYTES = 8 << 20
#: Long-running requests (CONCURRENT_METHODS) one connection may have in flight; more are refused, not queued.
MAX_CONCURRENT_PER_CLIENT = 8
#: Methods a socket peer may call before ``gateway.auth``: each reads k3code's own state and changes nothing (the
#: ``k3code doctor`` probe, listings, the settings view). Every other method needs an authenticated connection: a
#: turn (``prompt.submit``), a steer, an undo, attaching to or creating a session, a shell command, a mode or config
#: change, an attachment, a path listing, ... A method added to ``_HANDLERS`` is privileged until it is listed here.
#: The stdio client (the TUI's own child) is trusted.
OPEN_METHODS = frozenset(
    {
        "setup.status",
        "system.battery",
        "commands.catalog",
        "complete.slash",
        "input.detect_drop",
        "model.options",
        "tune.get",
        "settings.get",
        "config.get",
        "session.list",
        "session.active_list",
        "session.most_recent",
        "session.status",
        "session.usage",
        "session.control.read",
        "session.events.since",
        "session.events.stats",
        "subagent.list",
        "subagent.tail",
        "process.list",
        "delegation.status",
    }
)


def _requires_auth(method: str, params: Any) -> bool:
    if method == "browser.manage":  # status is a read; connect attaches to a CDP endpoint, disconnect drops it
        action = params.get("action") if isinstance(params, dict) else None
        return str(action or "status").strip().lower() != "status"
    return method not in OPEN_METHODS


#: Emitted for gateway.ready; the TUI repaints its palette from this.
_DEFAULT_SKIN = {
    "name": "k3code",
    "tool_prefix": "k3",
}

_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system.md"
#: How long an approval, plan or clarify request may wait for an answer before it is denied and the goal pauses.
APPROVAL_TIMEOUT_S = 1800.0


def _load_system_prompt() -> str:
    if _SYSTEM_PROMPT_PATH.is_file():
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return "You are a helpful coding assistant."


def _turn_status(result: Any) -> str | None:
    """The status of a finished turn task's result: ``_run_turn`` returns ``(status, text)``."""
    if isinstance(result, tuple) and result and isinstance(result[0], str):
        return result[0]
    return None


class TypedPrompt(str):
    """A prompt the user typed (``prompt.submit`` without ``automated``, a steering message, ``/bg <prompt>``).

    Only these are looked at for wake words and the ultracode mode; goal kicks, loop and cron ticks, automations and
    the TUI's own prompts (``automated``) are plain ``str``. The type travels with the text through
    ``pending_prompts`` and ``steer_queue``, so a prompt that waited behind a turn is still routed when it drains.

    ``paste_spans``: ``(start, end)`` offsets (code points) of text the user pasted rather than typed, which the TUI
    sends; a wake word in a pasted log or file is not the user asking for a mode."""

    paste_spans: tuple[tuple[int, int], ...]

    def __new__(cls, text: str, paste_spans: tuple[tuple[int, int], ...] = ()) -> TypedPrompt:
        self = super().__new__(cls, text)
        self.paste_spans = paste_spans
        return self


@dataclass
class _Route:
    """Where a typed prompt goes instead of a normal turn: the job (or the immediate reply) and what to announce."""

    command: str  #: ``ultra.progress`` command: ultracode, ultraplan or ultraresearch
    phase: str  #: ``wake word`` or ``ultracode is on``
    detail: str
    spec: JobSpec | dict[str, Any]  #: a dict is the command's immediate reply (usage / unavailable): no job


class LiveSession:
    """One active conversation: AgentLoop + router + cached state."""

    def __init__(self, session_id: str, stored: Any, server: GatewayServer) -> None:
        self.session_id = session_id
        self.stored = stored
        self.server = server
        self.system_prompt = _load_system_prompt()  # base prompt; per-turn extras via build_system_prompt
        self.loop: AgentLoop | None = None
        self.turn_task: asyncio.Task[None] | None = None
        self._tools_cache: dict[str, list[str]] | None = None  # banner info, see live_info
        self._skills_cache: dict[str, list[str]] | None = None
        self.streaming = False
        #: Serializes whole turns of this session (two prompts, a loop tick and a prompt, ... never interleave two
        #: AgentLoops on one conversation) and unattended runs' set-up/tear-down (see ServerRunner.run_prompt).
        self.turn_lock = asyncio.Lock()
        self.run_lock = asyncio.Lock()
        #: Prompts submitted while a turn was running; each runs as its own turn when the current one ends.
        self.pending_prompts: list[str] = []
        #: `!cmd` commands running now (shell.exec): exec_id -> the event shell.cancel sets
        self.user_shells: dict[str, asyncio.Event] = {}
        #: `!cmd` records (user messages) not yet in the transcript: a running turn's persist would overwrite them, so
        #: they wait for the turn's end or the next turn's start (GatewayServer._flush_shell_records)
        self.shell_records: list[dict[str, Any]] = []
        #: session.steer messages for the running turn: the live AgentLoop takes them before its next model call;
        #: what no loop took (a job ran, or the loop had already answered) runs as the next prompt.
        self.steer_queue: list[str] = []
        #: tool call ids of the model call in flight already announced with tool.start (see _announce_tool)
        self.announced_tools: set[str] = set()
        #: monotonic start of the model call in flight (its first router attempt), and the moment the loop began its
        #: next tool (the model call's end, then each tool's end: it runs them one after another), so the ``call`` /
        #: ``tool`` usage rows carry their wall time (``k3code stats`` shows where time goes)
        self.call_started: float | None = None
        self.tool_mark: float | None = None
        self.last_checkpoint = 0.0  # monotonic time of the last mid-turn persist (see GatewayServer._checkpoint_turn)
        self.persist_warned = False  # the "could not save" notification went out (once per session)
        self.checkpoint_task: asyncio.Task[None] | None = None  # the checkpoint write in flight, if any
        self.idle_since = time.monotonic()  # when the last turn ended (the idle sweeper stops netwatch after a while)
        #: Wall-clock start of the latest turn or job (time.time()); the agent view's "working N" counts from it.
        self.turn_started_wall = 0.0
        self.reasoning_effort: str | None = stored.meta.get("reasoning_effort")  # /effort
        #: "off" | "ultracode": ultracode as a session mode (/tune, /ultracode on|off, tune.set); kept in the meta
        self.ultra_mode: str = (
            stored.meta["ultra_mode"] if stored.meta.get("ultra_mode") in tune_cmd.ULTRA_MODES else "off"
        )
        self.todos: list[dict[str, Any]] = []
        self.todo_revision = 0
        #: The learned notes (learned.md, written by the background review) as this session's prompt shows them: taken
        #: when the prompt is first built, so a note written mid-session does not change the prompt (and make the
        #: provider re-write the whole conversation); None = read them again, as after a compaction or /clear.
        self.learned_notes: list[str] | None = None
        #: ... and the rest of what background writers change in the prompt (project facts, the user memory file the
        #: daily distill rewrites, accepted gotchas, the optimizer overlay, the project memory files, the MCP index),
        #: taken on the same terms
        self.prompt_frozen: dict[str, Any] = {}
        #: model calls a turn already made before a compaction in its middle restarted it (the max_turns cap is the
        #: user's limit on the task, not on each piece of it)
        self.turns_carried = 0
        #: The goal reviewer's summary of the conversation so far (autonomy.advisor.condensed_context).
        self.advisor_memo: dict[str, Any] = {}
        #: what this session's model has seen of each file: every loop of it (turns, tiers) checks write/edit against it
        self.file_stamps = FileStamps()
        #: ... seeded from the meta (see _persist_turn): a resumed, forked or daemon-restarted session would otherwise
        #: refuse the first edit of every file its history shows as read
        self.file_stamps.load(stored.meta.get("file_stamps"))
        self.ultra_dirty_noted = False  # the always-on ultracode told the user its checkout is dirty (once a spell)
        self.pending_approval: asyncio.Future[dict[str, Any]] | None = None
        self.pending_request_id: str | None = None
        # an invalid configured permission_mode fails this session's creation with a named error (an RPC reply), not
        # a bare ValueError from the constructor
        mode = (
            PermissionMode(stored.meta["mode"])
            if stored.meta.get("mode")
            else permission_mode_from_config("permission_mode", server.config.permission_mode)
        )
        self.perms = PermissionState(
            mode=mode,
            cwd=Path(stored.cwd or Path.cwd()),
            add_dirs=list(stored.meta.get("add_dirs") or []),
        )
        self.control: dict[str, Any] = {"goal": "", "loop": "", "heartbeat": "", "revision": 0, "updated_at": 0.0}
        if stored.meta.get("goal"):
            from k3code.goals import GoalState

            self.control["goal"] = GoalState.from_dict(stored.meta["goal"]).snapshot()
        #: Background/cron/loop sessions run bash inside the sandbox.
        self.background = bool(stored.meta.get("background"))
        #: one approval question at a time: sub-agents of one session can ask side by side, and the TUI shows one
        self.approval_lock = asyncio.Lock()
        self.stop_hook_running = False  # the Stop hook runs after message.complete; shutdown gives it a moment
        #: True while a /goal continuation runs: nobody is watching, so bash is sandboxed even in a foreground session.
        self.goal_continuation = False
        self.reliability: Reliability | None = None
        #: Why NetWatch did not start ("" while it runs): offline pause/resume is off for this session until it does.
        self.offline_protection_error = ""
        #: paused = waiting on the network/provider; the session still counts as working.
        self.paused = False
        self.paused_since = 0.0
        self.pause_text = ""
        #: Set when the loop guard or a budget stopped the turn; cleared on the next prompt.
        self.needs_input = False
        #: The turn stopped on a spend budget the user set (reliability.*): a persistent goal respects that stop.
        self.budget_stopped = False
        #: Failed or stalled goal turns in a row: the pause before the next retry grows with it (GoalManager.backoff).
        self.goal_retry_streak = 0
        #: Loop-guard stops of a goal's turns in a row, no successful turn between (see _goal_recovery); and what the
        #: guard stopped on in this turn (its key), "" when it did not.
        self.goal_loop_streak = 0
        self.loop_repeat = ""
        #: Tool calls the last turn made (None until one ran): a goal turn that calls none is slowed (goals.py).
        self.last_tool_calls: int | None = None
        #: (provider, model) of the latest router attempt, for usage rows.
        self.last_entry: tuple[str, str] = ("", "")
        #: M4a: tier and task kind of the call in flight (for usage rows); /scope override for the next task.
        self.last_tier = "main"
        self.current_kind = "interactive_turn"
        #: M1: id of the turn in flight (usage rows carry it, so per-turn totals add up); "" between turns.
        self.turn_id = ""
        self.scope_override: str | None = None
        #: Estimated tokens of the system prompt + tool schemas of this session's latest loop (0 = none built yet).
        self.overhead_tokens = 0
        #: monotonic time before which automatic compaction is not tried again (it failed: a summary call can take
        #: 90 s to fail, and a turn that waits that long on every prompt is worse than a long history)
        self.compact_retry_at = 0.0
        #: Estimated tokens of the conversation (not of the system prompt and the tools) a compaction left when that
        #: put the request at or above the limit, i.e. what cannot be folded (the kept tail, the summary) and what
        #: every request carries already pass it (0 = the last one did not). Automatic compaction then waits until the
        #: request has grown past compact_resume_at(): it summarized on every turn with nothing gained. Not stored;
        #: cleared when a request is below the limit again.
        self.compact_unfoldable = 0
        #: The user was told about that spell (once per spell, not on every turn).
        self.compact_unfoldable_noted = False
        #: Prompt tokens this session sent, and how many the provider read from its cache (the status bar's hit rate).
        self.prompt_tokens_total = 0
        self.cache_read_tokens_total = 0
        #: Tool results elided from this session's requests so far, and what the decision model noted of them (the
        #: loop of each turn shares them: see AgentLoop.share_elision); emptied when a compaction rewrites the history.
        self.elided: set[str] = set()
        self.elide_notes: dict[str, str] = {}
        #: Prompt tokens the provider counted / the estimate of the conversation part of that request (a moving
        #: average, kept within RATIO_MIN..RATIO_MAX): code, JSON and non-English text take far more tokens than the
        #: estimate says. _request_tokens applies it. ``token_ratio_model``: the model key it was measured for (a switch
        #: starts again); ``token_ratio_stale``: the next sample replaces it instead of joining the average;
        #: ``token_samples``: eligible calls seen since (sampled, see GatewayServer._calibrate). Kept in the meta.
        self.token_ratio = 1.0
        self.token_ratio_model = stored.model or ""
        self.token_ratio_stale = True
        self.token_samples = 0
        if isinstance(saved := stored.meta.get("token_ratio"), dict) and saved.get("model") == self.token_ratio_model:
            with contextlib.suppress(TypeError, ValueError):
                self.token_ratio = min(RATIO_MAX, max(RATIO_MIN, float(saved.get("ratio"))))
                self.token_ratio_stale = False
        #: Files read so far ((path, mtime, size, range) → (step, tool call id)) and the results a pointer to one needs
        #: kept whole (AgentLoop.share_reads); emptied where the history is rewritten, with ``elided``.
        self.reads: dict[tuple[Any, ...], tuple[int, str]] = {}
        self.pinned_reads: set[str] = set()
        # ... and what a resumed, forked or restarted session needs to send the requests the provider has cached
        _restore_elision(self)
        _restore_snapshot(self)
        #: /advisor text awaiting "accept" (kept out of the main context until then).
        self.pending_advisor: str = ""
        #: Tool installers run on each turn's registry (loop sessions add ``schedule_next``).
        self.extra_tools: list[Callable[[Any], None]] = []
        #: the SessionStart hooks ran for this session in this process
        self.hooks_started = False
        #: Outcome of the last turn (``completed`` / ``failed``); cleared when a new turn starts.
        self.run_result: str | None = None
        #: What the last turn ended with, for the cron/loop runners.
        self.last_error = ""
        self.last_exc: BaseException | None = None
        self.last_api_calls = 0
        #: Task kind for the next unattended turn (``loop_tick`` / ``cron_job`` / ``background_turn``).
        self.task_kind: str = ""
        #: /go after /ultraplan: {task, plan, path}; consumed by the next turn's scope gate.
        self.preapproved_plan: dict[str, Any] | None = None
        #: The scope gate's result for the turn last run (its verdict and approved plan), for a rerun of the prompt.
        self.turn_gate: GateResult | None = None

    def refresh_prompt_inputs(self) -> None:
        """Read the learned notes and the other background-written parts of the prompt afresh at the next turn (the
        conversation is being rewritten anyway: a compaction, /clear, a move to another project)."""
        self.learned_notes = None
        self.prompt_frozen.clear()
        self.stored.meta.pop("prompt_snapshot", None)

    @property
    def turn_in_flight(self) -> bool:
        """A turn owns this session: it streams, or its task still runs (scope gate, compaction, the goal judge, the
        goal check, the advisor: ``streaming`` is False there). Anything that starts a turn, or evicts or idles the
        session, must test this, not ``streaming``."""
        return self.streaming or (self.turn_task is not None and not self.turn_task.done())

    @property
    def state(self) -> str:
        """``working`` (also while paused), ``needs_input``, ``completed``/``failed`` (last turn) or ``idle``."""
        if self.needs_input or self.server.has_open_request(self.session_id):
            return "needs_input"
        if self.streaming:
            return "working"
        return self.run_result or "idle"

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, importance: str | None = None) -> None:
        """Send an event to every client attached to this session."""
        self.server.emit(event_type, payload, session=self, importance=importance)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.stored.messages

    @messages.setter
    def messages(self, value: list[dict[str, Any]]) -> None:
        self.stored.messages = value

    def set_mode(self, mode: PermissionMode) -> None:
        """Switch permission mode, persist it, and tell the client."""
        self.perms.mode = mode
        self.stored.meta["mode"] = mode.value
        self.server.store.save_meta_of(self.stored)
        self.emit("session.info", self.live_info())

    @property
    def history(self) -> list[Message]:
        """Stored messages as provider Messages, the leading system entry excluded (the loop builds a fresh one).

        A system message later in the conversation (the loop guard's note, a ``[learned]`` lesson) stays: the turn that
        wrote it sent it to the provider, so the next turn must send it at the same place, or its request differs from
        the cached one from there on.
        """
        out: list[Message] = []
        leading = True
        for m in self.stored.messages:
            role = m.get("role")
            if not role or (role == "system" and (leading or not m.get("content"))):
                continue
            leading = False
            out.append(
                Message(
                    role=role,
                    content=m.get("content"),
                    tool_call_id=m.get("tool_call_id"),
                    name=m.get("name"),
                    # Without these the next turn sent `tool` results with no assistant tool_calls: HTTP 400 on
                    # OpenAI-compatible and Anthropic providers, for every session that had used a tool.
                    tool_calls=[
                        ToolCall(
                            id=str(tc.get("id") or ""),
                            name=str(tc.get("name") or ""),
                            arguments=dict(tc.get("arguments") or {}),
                            raw_arguments=tc.get("raw_arguments"),
                        )
                        for tc in (m.get("tool_calls") or [])
                    ],
                )
            )
        return _answer_interrupted_calls(out)

    def live_info(self) -> dict[str, Any]:
        """SessionLiveInfo payload for session.create/resume/activate results."""
        config = self.server.config
        key = self.stored.model or config.default_model
        return {
            "model": _model_label(config, key),
            "model_key": key,
            "version": __version__,
            "tools": self._tools_info(),
            "skills": self._skills_info(),
            "provider": self.stored.provider,
            "reasoning_effort": self.reasoning_effort,
            "ultra_mode": self.ultra_mode,
            "approval_mode": self.perms.mode.value,
            "mode": self.perms.mode.value,
            "yolo": self.perms.mode == PermissionMode.YOLO,
            "add_dirs": list(self.perms.add_dirs),
            "cwd": _norm_cwd(self.stored.cwd),
            "title": self.stored.title,
            "stored_session_id": self.session_id,
            "running": self.streaming,
            "state": self.state,
            "paused": self.paused,
            "background": self.background,
            "agent_limits": clean_override(self.stored.meta.get("agent_limits")),
        }

    def _tools_info(self) -> dict[str, list[str]]:
        """Tool names by group for the TUI's banner (built once per session; MCP tools are added live)."""
        if self._tools_cache is None:
            reg = build_tool_registry()
            register_skill_tool(reg, self.perms.cwd, list(self.server.config.skills.roots))
            register_web_tools(reg, self.server.config)
            register_task_tools(reg, self.server, self, depth=1)
            groups: dict[str, list[str]] = {}
            for name in reg.names():
                groups.setdefault(_TOOL_GROUPS.get(name, "agent"), []).append(name)
            self._tools_cache = groups
        out = dict(self._tools_cache)
        mcp = [t.qualified for t in self.server.mcp.tools()] if self.server.mcp else []
        if mcp:
            out["mcp"] = mcp
        return out

    def _skills_info(self) -> dict[str, list[str]]:
        if self._skills_cache is None:
            try:
                found = skills_mod.discover(self.perms.cwd, list(self.server.config.skills.roots))
            except OSError:
                found = []
            self._skills_cache = {"skills": sorted(sk.name for sk in found)} if found else {}
        return self._skills_cache


#: ChainExhausted.last_reason -> the TUI's turn-failure code (tui/src/app/userMessages.ts TURN_CODE_COPY).
_FAILURE_CODES = {
    "auth": "auth",
    "quota": "billing",
    "rate_limit": "rate_limit",
    "bad_request": "model_not_found",
    "timeout": "timeout",
    "server": "server_error",
    "context_overflow": "context_overflow",
}


def _error_surface(exc: BaseException | None) -> dict[str, Any] | None:
    """What the TUI needs to explain a failed turn: a code it has copy for, and whether /retry can help."""
    if isinstance(exc, AllProvidersUnreachable):
        return {"layer": "endpoint", "retryable": True}
    if isinstance(exc, ContextOverflow):
        return {"code": "context_overflow", "retryable": True}
    if isinstance(exc, ChainExhausted):
        reason = exc.last_reason or "unknown"
        surface: dict[str, Any] = {"layer": "provider", "retryable": reason not in PERMANENT_REASONS}
        if reason in _FAILURE_CODES:
            surface["code"] = _FAILURE_CODES[reason]
        return surface
    if isinstance(exc, DiskGuardFull):
        return {"layer": "disk", "retryable": True}
    return {"layer": "gateway", "retryable": True} if exc is not None else None


_TOOL_GROUPS = {
    "read": "files",
    "write": "files",
    "edit": "files",
    "glob": "files",
    "grep": "files",
    "bash": "shell",
    "todo": "planning",
    "exit_plan": "planning",
    "web_search": "web",
    "web_fetch": "web",
}


def _model_label(config: Any, key: str) -> str:
    """The model id the first provider uses for ``key`` (what the user recognises), else the key itself."""
    specs = _resolve_model_specs(config, key) if config.providers else []
    first = specs[0] if specs else ""
    if isinstance(first, list):
        first = first[0] if first else ""
    return str(first or key)


#: How many recent messages stay when a session's older messages are folded into a summary. When that happens is
#: context_budget.compact_threshold: context.compact_at_ratio of the active model's window, or compact_at_tokens.
CONTEXT_DEFAULTS: dict[str, Any] = {"keep_messages": 8, "compact_input_chars": 60_000}
#: What the model is told when its turn goes on after a compaction in the middle of it.
COMPACTED_CONTINUATION = (
    "The context was compacted mid-task; continue where you stopped and do not repeat finished steps."
)
#: After a failed automatic compaction, seconds before the next turn tries again.
COMPACT_RETRY_AFTER_S = 120.0
#: Of the smallest context window the summary call can run on, this share is the most its input takes (the system
#: prompt and the summary itself are the rest, with room for the estimate being low).
SUMMARY_INPUT_SHARE = 0.4
#: The estimate of a request is corrected by the ratio of what the provider counted to what was estimated, within these
#: bounds, from requests whose conversation alone is estimated at CALIBRATE_MIN_TOKENS (a small one is mostly the system
#: prompt, and what a provider adds of its own would count for far too much), one eligible call in CALIBRATE_EVERY (each
#: sample estimates the whole conversation); a new sample counts RATIO_WEIGHT.
RATIO_MIN, RATIO_MAX = 0.5, 3.0
CALIBRATE_MIN_TOKENS = 4_000
CALIBRATE_EVERY = 4
RATIO_WEIGHT = 0.3
#: What a session keeps in its meta of the state its requests are built from, so that a resumed, forked or restarted
#: session sends the requests the provider has cached: the ids of the tool results (and call arguments) elided so far,
#: at most this many, the newest first (a session that elides more asks for the older ones again: they come back whole
#: until the request is long, and are all elided again at once), and the decision model's notes on them, up to this
#: many characters.
ELIDED_META_IDS = 2_000
ELIDE_NOTES_META_CHARS = 40_000
#: The prompt snapshot (the learned notes and the inputs frozen with them, see LiveSession.prompt_frozen) is kept for as
#: long as the longest provider cache (cache_marker "1h") may still hold the prompt built from it: a session resumed
#: later reads the files afresh, as it always did. It is not kept when it is over this many characters.
SNAPSHOT_MAX_AGE_S = 3_600
SNAPSHOT_META_CHARS = 60_000
#: A todo list as render_todos writes it (in a summary, where the model may have restated it).
TODO_BLOCK_RE = re.compile(r"Todos \(\d+/\d+ done\):\n(?:\[[ >x]\] .*(?:\n|$))*")
#: How often one turn is compacted and gone on after a context overflow (each needs the steps made since the last).
MAX_OVERFLOW_FOLDS = 10


def _estimate_tokens(
    messages: list[dict[str, Any]],
    tool_output_chars: int | None = None,
    elided: Collection[str] = (),
    window: int | None = None,
) -> int:
    """Rough size of what a conversation sends (see context_budget.text_tokens), tool calls included.

    Tool results are counted as the model receives them (head+tail clip to ``context.tool_output_chars``), so the
    stored full text does not trigger compaction early; everything else is counted as stored. ``elided``: the ids the
    loop has replaced by a marker in its requests (a result's own id; ``args:<id>`` for the file text of a call): they
    are counted as the marker, or a session that elides well kept compacting a request a tenth of the size.
    ``window``: the context window, which the loop holds one result to a share of (``clip:<id>`` in ``elided``: a result
    it cut down further to fit the request in the window).
    """
    tokens = 0
    for m in messages:
        c = m.get("content")
        if m.get("role") == "tool" and isinstance(c, str):
            if m.get("tool_call_id") in elided and len(c) > ELIDE_MIN_CHARS:
                c = elide_marker(m.get("name"), len(c))
            else:
                c = clip_for_model(m.get("name"), c, tool_output_chars or MAX_TOOL_RESULT_CHARS, window)
                if window and f"clip:{m.get('tool_call_id')}" in elided:
                    cut = result_chars(c, window, SHRINK_SHARE, SHRINK_FLOOR_CHARS)
                    c = shrink_result(m.get("name"), c, cut)
        tokens += text_tokens(c) if isinstance(c, str) else text_tokens(json.dumps(c, ensure_ascii=False)) if c else 0
        for tc in m.get("tool_calls") or []:
            # the name and the arguments once, as context_budget.message_tokens counts them (the whole call dict carried
            # the arguments twice when both the parsed and the raw form were stored)
            args = tc.get("raw_arguments") or json.dumps(tc.get("arguments") or {}, ensure_ascii=False)
            if (short := cut_call_text(tc.get("arguments"))) is not None:  # a cut-off call is sent as its start only
                args = json.dumps({"_unparsed": short}, ensure_ascii=False)
            if f"args:{tc.get('id')}" in elided and len(args) > ELIDE_MIN_CHARS:
                args = args[: ELIDE_MIN_CHARS // 4] + " " * 40  # the text of the call is a marker of about this size
            tokens += text_tokens(str(tc.get("name") or "") + args)
    return tokens


def _history_ids(messages: list[dict[str, Any]]) -> list[str]:
    """The ids the elision state can name, in the order of the history: a tool result by its call's id (and as
    ``clip:<id>`` when the loop cut it down), the text of a call's arguments as ``args:<id>`` (see _estimate_tokens)."""
    ids: list[str] = []
    for m in messages:
        if m.get("role") == "tool":
            ids.extend((str(m.get("tool_call_id") or ""), f"clip:{m.get('tool_call_id') or ''}"))
        ids.extend(f"args:{tc.get('id')}" for tc in m.get("tool_calls") or [])
    return ids


def _elision_meta(messages: list[dict[str, Any]], elided: Collection[str], notes: dict[str, str]) -> dict[str, Any]:
    """The meta keys that keep ``elided`` and ``notes`` (only ids the history still has, the newest ELIDED_META_IDS of
    them): ``{"elided": [...], "elide_notes": {...}}``, a key left out when there is nothing in it."""
    present = [i for i in _history_ids(messages) if i in elided][-ELIDED_META_IDS:]
    kept, size = {}, 0
    for i in reversed(present):
        if (note := notes.get(i)) and size + len(note) <= ELIDE_NOTES_META_CHARS:
            kept[i], size = note, size + len(note)
    return {**({"elided": present} if present else {}), **({"elide_notes": kept} if kept else {})}


def _restore_elision(session: LiveSession) -> None:
    """Give a session the elision state its meta kept (see _elision_meta), and pin again the results that an
    "unchanged since the read" answer of the history points at (they are never elided: the pointer would dangle)."""
    messages = session.stored.messages
    here = set(_history_ids(messages))
    saved = session.stored.meta.get("elided")
    if isinstance(saved, list):
        session.elided.update(i for i in saved if isinstance(i, str) and i in here)
    notes = session.stored.meta.get("elide_notes")
    if isinstance(notes, dict):
        session.elide_notes.update({k: v for k, v in notes.items() if isinstance(v, str) and k in here})
    for m in messages:
        if m.get("role") == "tool" and (hit := POINTER_CALL_RE.match(str(m.get("content") or ""))):
            session.pinned_reads.add(hit.group(1))


def _snapshot_meta(session: LiveSession) -> dict[str, Any] | None:
    """The session's prompt snapshot for its meta (see SNAPSHOT_MAX_AGE_S), or None when there is none or it is big."""
    frozen: dict[str, Any] = {}
    for key, value in session.prompt_frozen.items():
        if key == "project_memory" and isinstance(value, tuple) and len(value) == 2:
            frozen[key] = [value[0], [[f.scope, str(f.path), f.text] for f in value[1]]]
        elif isinstance(value, str):
            frozen[key] = value
    if session.learned_notes is None and not frozen:
        return None
    snapshot = {"at": time.time(), "learned": session.learned_notes, "frozen": frozen}
    return snapshot if len(json.dumps(snapshot, ensure_ascii=False)) <= SNAPSHOT_META_CHARS else None


def _restore_snapshot(session: LiveSession) -> None:
    """Give a session the prompt snapshot its meta kept, when it is recent (see SNAPSHOT_MAX_AGE_S)."""
    saved = session.stored.meta.get("prompt_snapshot")
    try:
        if not isinstance(saved, dict) or time.time() - float(saved["at"]) > SNAPSHOT_MAX_AGE_S:
            return
        learned = saved.get("learned")
        frozen: dict[str, Any] = {}
        for key, value in dict(saved.get("frozen") or {}).items():
            if key == "project_memory":
                frozen[key] = (str(value[0]), tuple(MemoryFile(str(a), Path(b), str(c)) for a, b, c in value[1]))
            elif isinstance(value, str):
                frozen[key] = value
        session.learned_notes = [str(x) for x in learned] if isinstance(learned, list) else None
        session.prompt_frozen.update(frozen)
    except (KeyError, TypeError, ValueError):
        logger.debug("the saved prompt snapshot of %s is unusable", session.session_id, exc_info=True)


def _socket_is_live(path: Path) -> bool:
    """True when something accepts connections on the Unix socket ``path`` (as opposed to a stale file)."""
    import socket as _socket

    probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return False
    finally:
        probe.close()


class Client:
    """One attached JSON-RPC peer (the stdio pipe, or one Unix-socket connection)."""

    def __init__(self, send: Callable[[str], None], name: str = "stdio", *, authenticated: bool = False) -> None:
        self.send = send
        self.name = name
        self.session_id: str | None = None
        self.closed = False
        self.pending_bytes = 0  # queued for the peer but not yet accepted by its socket (see MAX_CLIENT_BACKLOG)
        self.close_peer: Callable[[], None] | None = None  # drops the connection (socket clients)
        #: May call any method, not only OPEN_METHODS, and answer approvals: the stdio pipe, a socket peer after auth.
        self.authenticated = authenticated
        self.in_flight = 0  # running CONCURRENT_METHODS tasks of this client (capped at MAX_CONCURRENT_PER_CLIENT)


#: The client whose request is being handled (so replies and "current session" resolve per client).
_ctx_client: contextvars.ContextVar[Client | None] = contextvars.ContextVar("k3_client", default=None)
#: The session a turn task is running, so router/reliability events reach its clients.
_ctx_session: contextvars.ContextVar[LiveSession | None] = contextvars.ContextVar("k3_session", default=None)
#: Workspace the client named with ``slash.exec {cwd}`` (``/bg`` from a TUI attached to a daemon launched elsewhere).
_ctx_cwd: contextvars.ContextVar[str | None] = contextvars.ContextVar("k3_cwd", default=None)


def _in_throwaway_worktree(rules: list[Any]) -> bool:
    """Every path the rules name lies in a fan-out child's worktree (``.k3code/worktrees/<id>/``)."""
    return bool(rules) and all(
        r.tool in ("edit", "read") and "/.k3code/worktrees/" in str(r.pattern).replace("\\", "/") for r in rules
    )


class GatewayServer:
    """Owns the transports (stdio and/or Unix socket), the live sessions, the router and the session store."""

    def __init__(
        self,
        *,
        stdin: asyncio.StreamReader | None = None,
        stdout: Any = None,
        config: Settings | None = None,
        store: SessionStore | None = None,
    ) -> None:
        self.config = config or load_config(project_dir=default_project_dir())
        #: config keys the files held at the last read (apply_file_config resets the ones that have since gone)
        self._file_keys: set[str] = _file_keys(default_project_dir())
        self.store = store or SessionStore(self._home() / "sessions.db")
        self._stdin = stdin
        self._stdout = stdout
        self.commands: CommandRegistry = build_commands()
        self.live: dict[str, LiveSession] = {}
        self.mcp = McpManager(mcpjson.merged(self.config.mcp.servers, default_project_dir()))
        # one pooled client, cache and rate budget per gateway, shared by every session's web tools
        self.web_fetcher = WebFetcher.from_config(self.config.research, self.config.web)
        self.browser = BrowserManager.from_config(self.config)  # launched on first use, never at start-up
        self.goal_judge: Any = None  # test hook: async (goal, last_text, session) -> (verdict, reason)
        self.providers: list[Any] = []
        self._provider_sigs: list[dict[str, Any]] = []  # provider_signature of the entries self.providers came from
        self._retiring: dict[int, Any] = {}  # replaced providers not closed yet (id -> provider), see _retire_providers
        self.router: Router | None = None
        self.cooldowns = CooldownStore(path=self._home() / "cooldowns.json")
        self._running = False
        self._server_request_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        #: Server→client requests still unanswered: id → (session_id, frame). Re-sent on attach.
        self._open_requests: dict[str, tuple[str, str]] = {}
        self._stdio_client = Client(lambda line: self._write(line), authenticated=True)  # our parent's own pipe
        #: k3 panes (tuios) link: set only when this gateway runs inside a pane ($TUIOS_SOCKET + $TUIOS_PANE_ID)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.panes = self._make_panes()
        self.clients: list[Client] = [self._stdio_client]
        self._socket_server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()
        #: Ring buffer of recent events (for /debug dump); ``debug`` also logs each one.
        self.event_log: deque[dict[str, Any]] = deque(maxlen=500)
        self.debug = False
        #: Set by the daemon in restart-storm safe mode: no background work starts.
        self.background_paused = False
        self.safe_mode_notice = ""
        #: ``/daemon pause``: a persisted global halt (k3code.halt); loaded here so it survives a restart.
        self.halt: Halt | None = load_halt(self._home())
        #: Waits for a person (approvals, paused goals) survive a restart here until a client takes them.
        self.blockers = BlockerStore(self._home() / "blockers.db")
        self.approval_timeout_s = APPROVAL_TIMEOUT_S
        #: Set by a graceful stop: a turn cancelled now pauses its goal as "daemon restart" (boot resumes it).
        self.stopping = False
        self._client_seq = 0
        self.usage = UsageDB(self._home() / "usage.db")
        DAY_LEDGER.loader = day_loader(self.usage, self.config)  # a restart does not hand out the day's cap again
        DAY_LEDGER.date = ""  # and the count is read from the log on first use
        self.artifacts = ArtifactStore(self._home() / "artifacts.db")
        self._tiers: TierRouters | None = None
        self._router_cache: dict[str, TierRouters] = {}  # one TierRouters per model key (see _ensure_router)
        self._side_tasks: set[asyncio.Task[Any]] = set()  # fire-and-forget work (titles)
        #: work that belongs to a turn but outlives its start (MCP early start, plan critique); close() cancels it
        self._bg_tasks: set[asyncio.Future[Any]] = set()
        self._idle_sweeper: asyncio.Task[None] | None = None
        #: (provider, model) of the most recent router attempt on any task (side calls read it).
        self.last_attempt: tuple[str, str] = ("", "")
        self.model_caller = ModelCaller(
            self.tier_routers,
            self.config,
            self.usage,
            emit=lambda t, p: self.emit(t, p),
            last_attempt=lambda: self.last_attempt,
            turn_of=self._turn_of,
            charge=self._charge_side_call,
        )
        self.autonomy = PlanFirst(self)
        self.learning = LearningHub(self, self.autonomy.proposals)
        #: Automation engine (loops, cron, triggers); started by the daemon or on the first /loop|/schedule.
        self.automation: Any = None
        self.last_user_activity = time.time()
        self.subagents = SubagentManager(self)
        self.fanout = FanoutExecutor(self)
        self.ultra = Ultra(self)
        self.research = Research(self)
        self.research_tools: Any = None  # test seam: replaces the MCP/built-in search+fetch provider

    # ── session registry ──────────────────────────────────────────────

    @property
    def session(self) -> LiveSession | None:
        """The current client's session."""
        client = _ctx_client.get() or self._stdio_client
        return self.live.get(client.session_id) if client.session_id else None

    @session.setter
    def session(self, live: LiveSession | None) -> None:
        client = _ctx_client.get() or self._stdio_client
        if live is None:
            client.session_id = None
            return
        self.live[live.session_id] = live
        self.attach(client, live)

    def has_open_request(self, session_id: str) -> bool:
        """An approval/clarify request of this session is waiting for an answer (the session needs input)."""
        return any(sid == session_id for sid, _ in self._open_requests.values())

    async def close_live(self, sid: str, *, disposable_only: bool = False) -> dict[str, Any]:
        """Drop ``sid`` from the live registry (its stored copy stays resumable) unless it is in use: a turn owns it,
        it runs in the background, it waits for an answer, or a client is on it.

        ``disposable_only`` (its last client just left, or the caller switched away from it) also keeps every session
        with something in it or behind it: a message, an automation origin, a finished-run state, queued input, a
        running sub-agent, an active loop or automation bound to it. Its stored row stays either way (the session's
        settings live there, and an empty row is invisible to auto-resume and the project's earlier sessions).
        """
        live = self.live.get(sid)
        if live is None:
            return {"closed": False, "reason": "not live"}
        if (
            live.turn_in_flight
            or live.background
            or live.needs_input
            or self.has_open_request(sid)
            or any(c.session_id == sid for c in self.clients)
        ):
            return {"closed": False, "reason": "still in use"}
        if disposable_only and (
            live.stored.messages
            or live.state != "idle"
            or live.stored.meta.get("origin") == "automation"
            or live.pending_prompts
            or live.steer_queue
            or any(not h.done for h in self.subagents.for_session(sid))
            or (self.automation is not None and self.automation.bound_to(sid))
        ):
            return {"closed": False, "reason": "not disposable"}
        self.live.pop(sid, None)
        self.learning.forget_session(sid)  # the failures it was waiting on a retry for
        await tool_jobs.reap(sid)  # background bash jobs end with their session
        if live.reliability is not None:
            await live.reliability.stop()
        return {"closed": True}

    def attach(self, client: Client, live: LiveSession, *, replay_delay: float = 0.0) -> None:
        """Point ``client`` at ``live`` and replay any approvals it is still waiting on.

        ``replay_delay`` > 0 sends them a moment later, so a client that resets its view when the
        ``session.activate``/``session.resume`` response arrives does not drop the replayed dialog.
        """
        client.session_id = live.session_id

        def replay() -> None:
            for req_id, (sid, frame) in list(self._open_requests.items()):
                if sid == live.session_id and client.session_id == live.session_id:
                    client.send(frame)
                    logger.debug("re-sent open request %s to %s", req_id, client.name)

        if replay_delay > 0:
            asyncio.get_running_loop().call_later(replay_delay, replay)
        else:
            replay()

    def live_for(self, stored: Any) -> LiveSession:
        """The running LiveSession for a stored session (created on first use)."""
        live = self.live.get(stored.session_id)
        if live is None:
            live = LiveSession(stored.session_id, stored, self)
            self.live[stored.session_id] = live
        return live

    @property
    def sessions(self) -> Any:
        """Mapping-like view over live sessions (``get(session_id)``) for command handlers."""
        server = self

        class _View:
            def get(self, session_id: str | None) -> LiveSession | None:
                return server._session_for(session_id)

        return _View()

    @staticmethod
    def _home() -> Path:
        return Path(os.environ.get("K3CODE_HOME", str(Path.home() / ".k3code"))).expanduser()

    # ── transport ─────────────────────────────────────────────────────

    def _make_panes(self) -> Any:
        from k3code.integrations.panes import PaneLink

        return PaneLink.from_env(inject=self._pane_inject)

    def _pane_inject(self, req_id: str, method: str, result: dict[str, Any]) -> None:
        """The tuios Inbox answered a request: resolve it as if the TUI had (callable from any thread)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def resolve() -> None:
            sid = (self._open_requests.get(req_id) or ("", ""))[0]
            if req_id not in self._server_request_futures:
                return  # already answered in the pane
            self._resolve_server_request(req_id, {"result": result})
            cancel = encode_server_request(
                "request.cancel", {"id": req_id, "method": method, "reason": "answered in the Inbox"}
            )
            for c in self.clients:
                if c.session_id == sid and c is self._stdio_client:
                    self._send(c, cancel)

        loop.call_soon_threadsafe(resolve)

    def _write(self, line: str) -> None:
        """One JSON frame to stdout (the stdio client's sink). Only frames ever land here."""
        if self.panes is not None:
            self.panes.on_server_line(line)
        out = self._stdout
        if out is None:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        elif hasattr(out, "write"):
            out.write(line + "\n")
            out.flush()

    def _send(self, client: Client, line: str) -> None:
        if client.closed:
            return
        try:
            client.send(line)
        except Exception:  # noqa: BLE001 - a dead peer must never break the sender
            logger.debug("send to %s failed", client.name)

    def _targets(self, session: LiveSession | None) -> list[Client]:
        """Who should see an event: the session's clients; else the requester; else everyone."""
        session = session or _ctx_session.get()
        if session is not None:
            return [c for c in self.clients if c.session_id == session.session_id and not c.closed]
        current = _ctx_client.get()
        if current is not None and not current.closed:
            return [current]
        return [c for c in self.clients if not c.closed]

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        session: LiveSession | None = None,
        importance: str | None = None,
    ) -> None:
        """Send a server→client event frame (and remember it for ``/debug``)."""
        payload = payload or {}
        sess = session or _ctx_session.get()
        record = {
            "ts": time.time(),
            "type": event_type,
            "session": sess.session_id if sess else None,
            "importance": importance,
            "payload": payload,
        }
        self.event_log.append(record)
        if self.debug:
            logger.info("event %s", json.dumps({k: v for k, v in record.items() if k != "ts"}, default=str)[:2000])
        line = encode_event(event_type, payload, importance)
        for client in self._targets(session):
            self._send(client, line)

    def broadcast(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Send an event to every attached client, whatever session it is looking at (strip, status badge)."""
        line = encode_event(event_type, payload or {}, None)
        for client in self.clients:
            if not client.closed:
                self._send(client, line)

    def broadcast_active_list(self) -> None:
        self.broadcast("session.active_list", {"sessions": self._active_rows(None)})

    async def ensure_automation(self) -> Any:
        """The automation engine, started on first use (the daemon starts it eagerly)."""
        if self.automation is None:
            from k3code.automation.engine import AutomationEngine

            self.automation = AutomationEngine(self)
            await self.automation.start()
        return self.automation

    def _reply(self, client: Client, line: str) -> None:
        self._send(client, line)

    # ── lifecycle ─────────────────────────────────────────────────────

    async def serve(self, *, stdio: bool = True, socket_path: Path | str | None = None) -> None:
        """Serve stdio until EOF and/or a Unix socket until :meth:`request_stop`."""
        self._running = True
        if not stdio:
            self.clients.remove(self._stdio_client)
        self._idle_sweeper = asyncio.get_running_loop().create_task(self._idle_sweep_loop(), name="k3-idle-sweeper")
        try:
            if socket_path is not None:
                await self.start_socket(socket_path)
            if stdio:
                await self._serve_stdio()
                if socket_path is None:
                    self.shutdown()
                    return
            await self._stop.wait()
            await self.stop_socket()
            self.shutdown()
        finally:
            self._idle_sweeper.cancel()

    async def _serve_stdio(self) -> None:
        loop = asyncio.get_running_loop()
        reader = self._stdin
        if reader is None:
            reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
            await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

        self._send_ready(self._stdio_client)

        while self._running:
            line = await self._read_frame(reader, self._stdio_client)
            if line is None:
                continue
            if not line:
                logger.info("stdin EOF; gateway stdio detached")
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            await self._take_frame(text, self._stdio_client, self._side_tasks)

    async def start_socket(self, path: Path | str) -> None:
        """Listen on a Unix socket: one JSON-RPC connection per client, sessions shared."""
        path = Path(path)
        gw_auth.prepare_socket_dir(path)  # the default run dir is made 0700; a custom one is only checked
        if path.exists():
            if _socket_is_live(path):
                raise RuntimeError(f"{path} is served by another process; not taking it over")
            path.unlink()  # stale socket from a crashed daemon
        # The token exists before the socket does: a client that sees the socket can authenticate.
        self._auth_token = gw_auth.write_token(path)
        old_umask = os.umask(0o077)  # the socket is born 0600: no window in which another user can connect
        try:
            self._socket_server = await asyncio.start_unix_server(
                self._on_connect, path=str(path), limit=MAX_FRAME_BYTES
            )
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)
        self.socket_path = path
        self._socket_ino = os.stat(path).st_ino  # so stop_socket only removes the socket this process created

    async def stop_socket(self) -> None:
        if self._socket_server is not None:
            self._socket_server.close()
            # wait_closed() blocks until every accepted connection has finished (CPython >= 3.12), and close() does
            # not touch existing ones: a graceful stop hung while any TUI was attached, systemd SIGKILLed the daemon
            # after 90 s and the shutdown work (persisting turns, stopping automations) never ran.
            for client in list(self.clients):
                if client.close_peer is not None:
                    client.closed = True
                    with contextlib.suppress(Exception):
                        client.close_peer()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._socket_server.wait_closed(), timeout=3.0)
            self._socket_server = None
        with contextlib.suppress(Exception):
            path = Path(getattr(self, "socket_path", ""))
            if path.exists() and os.stat(path).st_ino == getattr(self, "_socket_ino", None):
                path.unlink()
        if self._auth_token is not None and hasattr(self, "socket_path"):
            gw_auth.remove_token(self.socket_path, self._auth_token)

    socket_path: Path
    #: The token socket clients present with ``gateway.auth`` (None until a socket is served).
    _auth_token: str | None = None

    def request_stop(self) -> None:
        self.stopping = True
        self._stop.set()

    @property
    def halted(self) -> bool:
        """True while ``/daemon pause`` holds: no model turn, loop tick, job or sub-agent may start."""
        return self.halt is not None

    def _halt_payload(self) -> dict[str, Any]:
        return {
            "text": "Daemon halted (/daemon pause): turns, loops, jobs and sub-agents are stopped. "
            "/daemon resume to continue.",
            "level": "warning",
            "kind": "daemon",
            "key": "k3.halt",
        }

    async def halt_daemon(self, reason: str = "/daemon pause") -> int:
        """Global halt: persist it, pause every active goal, stop running turns and sub-agents.

        Returns how many running turns were stopped. Loops and jobs are not cancelled; they wait (see
        AutomationEngine._online) and continue after ``/daemon resume``.
        """
        self.halt = set_halt(self._home(), reason)
        stopped = 0
        for live in list(self.live.values()):
            mgr = self.goal_manager(live)
            if mgr.is_active():  # paused first, so the cancelled turn's own handler does not overwrite the reason
                mgr.pause("halted")
                self.emit_goal(live)
            if live.turn_task is not None and not live.turn_task.done():
                await self.interrupt_turn(live.session_id)
                stopped += 1
        for h in list(self.subagents.handles.values()):
            self.subagents.interrupt(h.id)
        self.broadcast("notification.show", self._halt_payload())
        return stopped

    def resume_daemon(self) -> bool:
        """``/daemon resume``: clear the halt and the restart-storm safe mode; returns whether either was set.

        Goals that the halt paused stay paused (``/goal resume``): nothing restarts unattended work by itself.
        """
        was_set = self.halted or self.background_paused
        clear_halt(self._home())
        self.halt = None
        self.background_paused = False
        self.safe_mode_notice = ""
        self.broadcast("notification.clear", {"key": "k3.safe_mode"})
        self.broadcast("notification.clear", {"key": "k3.halt"})
        return was_set

    #: A peer that stopped reading (SIGSTOPped TUI, hung ssh) is dropped once this many bytes are queued for it.
    MAX_CLIENT_BACKLOG = 8 * 1024 * 1024
    #: Methods whose handler may wait for the client's own answer (clarify / approval): run as tasks, so the read loop
    #: keeps reading and can deliver that answer. Awaited inline they deadlocked their own connection. shell.exec runs
    #: for minutes: inline, it held every other frame of its client (session.interrupt, shell.cancel) until it ended.
    CONCURRENT_METHODS = frozenset({"command.dispatch", "slash.exec", "shell.exec"})

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        client = Client(lambda line: None, name=f"socket#{self._client_seq + 1}")

        def drop(reason: str) -> None:
            if not client.closed:
                logger.warning("dropping client %s: %s", client.name, reason)
            client.closed = True
            if client in self.clients:
                self.clients.remove(client)
            with contextlib.suppress(Exception):
                # abort(), not close(): close() flushes the buffered data first, which a stalled peer never allows
                writer.transport.abort()

        def send(line: str) -> None:
            if client.closed:
                return
            data = (line + "\n").encode("utf-8")
            client.pending_bytes += len(data)
            if client.pending_bytes > self.MAX_CLIENT_BACKLOG:
                # transport.write() on a stalled peer is O(backlog) per call (CPython 3.12) and its buffer never
                # shrinks: every emit of every session got slower until the daemon was restarted.
                drop(f"{client.pending_bytes // 1024} KiB of events queued, the peer is not reading")
                return
            queue.put_nowait(data)

        async def pump() -> None:
            while True:
                data = await queue.get()
                writer.write(data)
                await writer.drain()  # waits while the peer is slow: the backlog then grows in `queue` (cheap, bounded)
                client.pending_bytes -= len(data)

        client.send = send
        client.close_peer = lambda: drop("daemon stopping")
        self._client_seq += 1
        client.name = f"socket#{self._client_seq}"
        self.clients.append(client)
        logger.info("client %s attached", client.name)
        pump_task = asyncio.get_running_loop().create_task(pump(), name=f"k3-pump-{client.name}")
        tasks: set[asyncio.Task[None]] = set()
        try:
            self._send_ready(client)
            # The attach snapshot: every live session and its state, as an event for this client only.
            self._send(client, encode_event("session.active_list", {"sessions": self._active_rows(None)}))
            if self.safe_mode_notice:
                self._send(
                    client,
                    encode_event(
                        "notification.show",
                        {"text": self.safe_mode_notice, "level": "warning", "kind": "daemon", "key": "k3.safe_mode"},
                        "essential",
                    ),
                )
            if self.halted:
                self._send(client, encode_event("notification.show", self._halt_payload(), "essential"))
            for row in self.blockers.pending():  # blockers no client has taken yet: hand them over now
                payload = {
                    "text": row["text"],
                    "level": row["level"],
                    "kind": row["kind"],
                    "key": f"k3.blocker.{row['id']}",
                    "session_id": row["session_id"],
                    "blocker_id": row["id"],
                }
                self._send(client, encode_event("notification.show", payload, "essential"))
                self.blockers.mark_delivered(row["id"])
            while not client.closed:
                line = await self._read_frame(reader, client)
                if line is None:
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                await self._take_frame(text, client, tasks)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            client.closed = True
            for task in list(tasks):
                task.cancel()
            pump_task.cancel()
            if client in self.clients:
                self.clients.remove(client)
            logger.info("client %s detached; its sessions keep running", client.name)
            with contextlib.suppress(Exception):
                writer.close()
            # Every TUI start creates a session so the prompt is usable; one left empty and idle by its last client
            # (TUI exit, crash, kill -9: the socket just drops) was never closed, because session.close refuses while
            # its caller is still attached. Only the live entry is dropped; the stored row stays. Not during a daemon
            # stop: close() iterates the registry across awaits.
            if client.session_id and not self.stopping:
                try:
                    if (await self.close_live(client.session_id, disposable_only=True))["closed"]:
                        logger.info("closed empty session %s: its last client left", client.session_id)
                except Exception:
                    logger.exception("closing session %s after its last client left failed", client.session_id)

    async def _read_frame(self, reader: asyncio.StreamReader, client: Client) -> bytes | None:
        """The next line from ``reader`` (b"" at EOF), or None for one longer than MAX_FRAME_BYTES: that frame is
        answered with an error and dropped, the connection stays up. Before, any line over asyncio's 64 KiB default
        (a pasted log as a prompt) raised out of the read loop: the stdio gateway exited, a socket client was cut."""
        try:
            return await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as e:
            rest_pending = "not found" in str(e)  # asyncio dropped the buffered part; the line's tail is still coming
        limit = f"{MAX_FRAME_BYTES >> 20} MiB" if MAX_FRAME_BYTES >= 1 << 20 else f"{MAX_FRAME_BYTES} bytes"
        self._reply(client, encode_error(None, INVALID_REQUEST, f"frame longer than {limit}: dropped"))
        while rest_pending:  # skip the tail, or it would be parsed as frames of its own
            try:
                tail = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError) as e:
                rest_pending = "not found" in str(e)
                continue
            if not tail:
                return b""
            rest_pending = not tail.endswith(b"\n")
        return None

    async def _take_frame(self, text: str, client: Client, tasks: set[asyncio.Task[Any]]) -> None:
        """Parse a frame once and handle it: inline, or as a task for CONCURRENT_METHODS (they may wait for this very
        client's answer to a clarify or approval, so the read loop must stay free). At most MAX_CONCURRENT_PER_CLIENT
        such tasks run per client; one more is refused with an error rather than queued, which could deadlock."""
        try:
            parsed = decode_frame(text)
            obj, err = parsed
            # an invalid frame (err set) goes to _guarded_handle, which answers it; `method` is only trusted without err
            if obj is not None and err is None and obj.get("method") in self.CONCURRENT_METHODS:
                if client.in_flight >= MAX_CONCURRENT_PER_CLIENT:
                    msg = f"too many concurrent requests on this connection (at most {MAX_CONCURRENT_PER_CLIENT})"
                    self._reply(client, encode_error(obj.get("id"), TOO_MANY_REQUESTS, msg))
                    return
                client.in_flight += 1

                def finished(_task: asyncio.Task[Any]) -> None:
                    client.in_flight -= 1

                task = asyncio.get_running_loop().create_task(self._guarded_handle(text, client, parsed))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                task.add_done_callback(finished)
                return
            await self._guarded_handle(text, client, parsed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad frame must never end the read loop (it ends the whole gateway)
            logger.exception("could not take a frame")

    async def _guarded_handle(
        self, text: str, client: Client | None, parsed: tuple[dict[str, Any] | None, str | None] | None = None
    ) -> None:
        try:
            await self._handle_line(text, client, parsed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("unhandled error processing frame")

    def _active_rows(self, current: str | None) -> list[dict[str, Any]]:
        rows = []
        for s in sorted(self.live.values(), key=lambda x: x.stored.updated_at, reverse=True):
            rows.append(
                {
                    "current": s.session_id == current,
                    "id": s.session_id,
                    "last_active": s.stored.updated_at,
                    "message_count": len(s.stored.messages),
                    "model": s.stored.model,
                    "preview": (s.stored.title or "")[:120],
                    "session_key": s.session_id,
                    "started_at": s.stored.created_at,  # when the stored session was created: age and sorting only
                    # Elapsed time of the turn in flight (also while paused or waiting on an approval); None between
                    # turns, so a resumed or finished session shows no time rather than its age since creation.
                    "turn_started_at": s.turn_started_wall if s.streaming else None,
                    "status": s.state,
                    "state": s.state,
                    "paused": s.paused,
                    "offline_protection": not s.offline_protection_error,
                    "background": s.background,
                    "origin": s.stored.meta.get("origin", ""),
                    "title": s.stored.title or "Session",
                }
            )
        for h in self.subagents.handles.values():  # sub-agent / fan-out children share the strip
            if h.status not in ("queued", "running"):
                continue
            rows.append(
                {
                    "current": False,
                    "id": h.id,
                    "last_active": h.started_wall,
                    "message_count": h.tool_count,
                    "model": h.model or h.tier,
                    "preview": h.description[:120],
                    "session_key": h.id,
                    "started_at": h.started_wall,
                    "status": h.status,
                    "state": h.status,
                    "paused": False,
                    "background": True,
                    "origin": "subagent",
                    "parent_id": h.parent_sid,
                    "title": h.description[:60] or "Sub-agent",
                }
            )
        return rows

    #: A session idle this long stops its reliability bundle (netwatch tasks); the next turn re-arms it.
    IDLE_RELIABILITY_S = 120.0
    IDLE_SWEEP_EVERY_S = 30.0

    async def _idle_sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.IDLE_SWEEP_EVERY_S)
            try:
                await self.sweep_idle_reliability()
            except Exception:
                logger.exception("idle reliability sweep failed")

    async def sweep_idle_reliability(self, now: float | None = None) -> int:
        """Stop the NetWatch of sessions that have been idle for IDLE_RELIABILITY_S; returns how many were stopped.

        Every live session owns a bundle with a started NetWatch (two forever-tasks: a probe loop and an nmcli
        watcher). Sessions pile up in ``server.live`` (each TUI launch, each /new), so an idle daemon spent more
        CPU and spawned more processes the longer it had served.
        """
        now = time.monotonic() if now is None else now
        stopped = 0
        for live in list(self.live.values()):
            rel = live.reliability
            if rel is None or not rel._started or live.turn_in_flight or live.pending_approval is not None:
                continue
            if now - live.idle_since < self.IDLE_RELIABILITY_S:
                continue
            try:
                await rel.stop()  # re-armed by _reliability_for on the session's next turn
            except Exception:
                logger.warning("session %s: stopping its idle NetWatch failed", live.session_id, exc_info=True)
                continue
            stopped += 1
        return stopped

    def shutdown(self) -> None:
        self._running = False
        for live in self.live.values():
            live.pending_approval = None
        for fut in self._server_request_futures.values():
            if not fut.done():
                fut.cancel()
        self._server_request_futures.clear()
        self._open_requests.clear()

    async def close(self) -> None:
        self.stopping = True
        pending = list(self._bg_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.automation is not None:
            await self.automation.stop()
        await self.web_fetcher.aclose()
        await self.browser.close()
        # a Stop hook that is running after its turn's answer was sent gets a moment to finish (quitting right after an
        # answer used to kill it); a hook that outlasts the grace is killed with its turn below
        finishing = [
            live.turn_task
            for live in self.live.values()
            if live.stop_hook_running and live.turn_task is not None and not live.turn_task.done()
        ]
        if finishing:
            await asyncio.wait(finishing, timeout=self.STOP_HOOK_GRACE_S)
        cancelled = []
        for live in self.live.values():
            if live.turn_task is not None and not live.turn_task.done():
                live.turn_task.cancel()
                cancelled.append(live.turn_task)
        if cancelled:  # let each turn persist what it has (its finally block) before the store is closed below
            await asyncio.wait(cancelled, timeout=5.0)
        with contextlib.suppress(Exception):
            await tool_jobs.REGISTRY.reap_all()  # no background bash job outlives the daemon
        for live in self.live.values():
            if live.reliability is not None:
                try:
                    await live.reliability.stop()
                except Exception:
                    logger.warning("session %s: stopping its reliability bundle failed", live.session_id, exc_info=True)
        with contextlib.suppress(Exception):
            await self.learning.aclose()  # cancel the spawned learning tasks, then close the decision log
        await self.mcp.close()
        await self._close_retired()
        for p in self.providers:
            await p.aclose()
        self.store.close()
        self.usage.close()
        self.blockers.close()

    def _send_ready(self, client: Client) -> None:
        self._send(
            client,
            encode_event("gateway.ready", {"skin": _DEFAULT_SKIN, "change_events": [], "replay_epoch": 1}),
        )

    # ── frame handling ────────────────────────────────────────────────

    async def _handle_line(
        self, text: str, client: Client | None = None, parsed: tuple[dict[str, Any] | None, str | None] | None = None
    ) -> None:
        """Handle one frame; ``parsed`` is ``decode_frame(text)`` when the read loop already did it."""
        client = client or self._stdio_client
        self._loop = asyncio.get_running_loop()
        if self.panes is not None and client is self._stdio_client:
            self.panes.on_client_line(text)
        token = _ctx_client.set(client)
        try:
            await self._dispatch_line(text, client, parsed)
        finally:
            _ctx_client.reset(token)

    async def _dispatch_line(
        self, text: str, client: Client, parsed: tuple[dict[str, Any] | None, str | None] | None = None
    ) -> None:
        obj, err = parsed if parsed is not None else decode_frame(text)
        if err is not None:
            kind = PARSE_ERROR if err.startswith("parse error") else INVALID_REQUEST
            if err.startswith("invalid params"):
                kind = INVALID_PARAMS
                if obj is not None and "id" not in obj:
                    return  # a notification gets no reply, not even an error
            self._reply(client, encode_error(reply_id(obj), kind, err))
            return
        assert obj is not None
        req_id = obj.get("id")
        method = obj.get("method")
        params = obj.get("params") or {}

        if method is None:
            # A result frame answering one of our server→client requests (approvals among them): only from a peer
            # that may also change permissions, or any same-user process could approve a pending command.
            if client.authenticated:
                self._resolve_server_request(req_id, obj)
            else:
                logger.warning("ignoring an answer to %s from unauthenticated client %s", req_id, client.name)
            return

        if req_id is None:
            # Notification: nothing to answer. M1 has no notification methods.
            logger.debug("ignoring notification %s", method)
            return

        if method == gw_auth.AUTH_METHOD:
            self._authenticate(client, req_id, params)
            return
        handler = _HANDLERS.get(method)
        if handler is None:
            self._reply(client, encode_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}"))
            return
        if not client.authenticated and _requires_auth(method, params):
            msg = f"{method} needs an authenticated connection (send {gw_auth.AUTH_METHOD} with the daemon token first)"
            self._reply(client, encode_error(req_id, UNAUTHORIZED, msg))
            return

        try:
            result = await handler(self, params)
            reply = encode_response(req_id, result)  # inside the try: a result that does not encode still answers
        except _InvalidParams as e:
            self._reply(client, encode_error(req_id, INVALID_PARAMS, str(e)))
        except Exception as e:  # noqa: BLE001 - one bad method must not kill the gateway
            logger.exception("method %s failed", method)
            self._reply(client, encode_error(req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}"))
        else:
            self._reply(client, reply)

    def _authenticate(self, client: Client, req_id: Any, params: Any) -> None:
        """``gateway.auth {token}``: mark the connection authenticated when the token matches this daemon's."""
        given = params.get("token") if isinstance(params, dict) else None
        expected = self._auth_token
        if expected is not None and isinstance(given, str) and hmac.compare_digest(given.encode(), expected.encode()):
            client.authenticated = True
            self._reply(client, encode_response(req_id, {"ok": True}))
            return
        logger.warning("client %s sent a wrong gateway token", client.name)
        self._reply(client, encode_error(req_id, UNAUTHORIZED, "wrong or missing gateway token"))

    def _resolve_server_request(self, req_id: Any, obj: dict[str, Any]) -> None:
        self._open_requests.pop(str(req_id), None)
        fut = self._server_request_futures.pop(str(req_id), None)
        if fut is None or fut.done():
            return
        if "error" in obj:
            fut.set_exception(RuntimeError(str(obj["error"].get("message", "client error"))))
        else:
            fut.set_result(obj.get("result") or {})

    # ── server→client requests ────────────────────────────────────────

    async def _ask_client(self, method: str, params: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Send an approval/clarify/sudo/secret request to the session's clients and await the answer.

        With nobody attached the request stays open (a detached background session
        waits) and is re-sent when a client attaches to the session.
        """
        req_id = next_request_id(method)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._server_request_futures[req_id] = fut
        params = {"session_id": session_id, **params}
        frame = encode_server_request(method, params, req_id=req_id)
        self._open_requests[req_id] = (session_id, frame)
        attached = [c for c in self.clients if c.session_id == session_id]
        for client in attached:
            self._send(client, frame)
        if not attached:  # nobody can answer right now: keep the request visible across a restart
            self.notify_blocker(
                session_id,
                f"Waiting for an answer: {params.get('command') or method}",
                level="info",
                kind="approval",
                key=f"k3.approval.{req_id}",
            )
        try:
            return await asyncio.wait_for(fut, self.approval_timeout_s)
        except TimeoutError:
            self._open_requests.pop(req_id, None)
            self._server_request_futures.pop(req_id, None)
            cancel = encode_server_request("request.cancel", {"id": req_id, "method": method, "reason": "timeout"})
            for client in self.clients:
                if client.session_id == session_id:
                    self._send(client, cancel)
            self._block_on_timeout(session_id, method)
            raise ApprovalTimeout(f"no answer to {method} within {int(self.approval_timeout_s)} s") from None
        except asyncio.CancelledError:
            self._open_requests.pop(req_id, None)
            self._server_request_futures.pop(req_id, None)
            # The turn was interrupted while waiting: tell the client to stop showing it.
            live = self.live.get(session_id)
            self.emit(
                "notification.show",
                {"text": "Request cancelled", "level": "info", "kind": "info", "key": req_id},
                session=live,
            )
            cancel = encode_server_request("request.cancel", {"id": req_id, "method": method, "reason": "interrupted"})
            for client in self.clients:
                if client.session_id == session_id:
                    self._send(client, cancel)
            raise

    def _block_on_timeout(self, session_id: str, method: str) -> None:
        """An unanswered request timed out: the goal of that session pauses with the reason, and a blocker is stored."""
        live = self.live.get(session_id)
        minutes = int(self.approval_timeout_s // 60) or 1
        text = f"No answer to the {method} request within {minutes} min. Answer it, then /goal resume."
        if live is not None:
            mgr = self.goal_manager(live)
            if mgr.is_active():
                mgr.pause(f"{method} timeout")
                self.emit_goal(live)
        self.notify_blocker(session_id, text, level="warning", kind="approval", key=f"k3.approval.timeout.{session_id}")

    # ── router wiring ─────────────────────────────────────────────────

    def _ensure_router(self, model: str | None = None) -> None:
        """Provider chain + routers, built once per provider config and once per model key.

        Sessions with different model keys (a cron job with ``model:`` next to an interactive session) used to
        rebuild the whole stack on every alternating turn: new httpx clients, a new cooldown store, a stray temp
        dir per claude-cli provider, none of the old ones closed. Routers are now cached per key; when the provider
        config itself is replaced (/config reload) only the blocks that changed get a new provider (see
        ``_swap_providers``).
        """
        key = model or self.config.default_model
        # _router_cfg None: a router injected from outside (tests) is taken as is
        current = self._router_cfg is None or self._router_cfg is self.config.providers
        if self.router is not None and self._chain_key == key and current:
            return
        if self._router_cfg is not self.config.providers:  # first build, or the provider config was replaced
            self._swap_providers()
            self._router_cache = {}
            self.__dict__.pop("_oneshot_routers", None)  # one-shot routers hold chains with the old model specs
            self._router_cfg = self.config.providers
        tiers = self._router_cache.get(key)
        if tiers is None:
            tiers = self._router_cache[key] = TierRouters(
                self.providers, self.config, cooldowns=self.cooldowns, on_event=self._on_router_event, main_key=key
            )
        self._tiers = tiers
        self.router = tiers.get(Tier.MAIN)
        self._chain_key = key

    def _swap_providers(self) -> None:
        """Providers for ``config.providers``: a block whose settings did not change keeps its provider.

        A turn keeps the router it started with, and with it these provider objects: closing them under it made its
        next call fail with "client has been closed", and a claude-cli provider takes its warm processes down. So
        only a changed or removed block is rebuilt and retired. The cooldown store stays: its keys name the endpoint,
        and an auth cooldown is tied to the key's fingerprint, so a replaced key does not inherit it.
        """
        entries = self.config.providers
        sigs = [provider_signature(e) for e in entries]
        spare = list(zip(self._provider_sigs, self.providers, strict=False))
        fresh: list[Any] = []
        for entry, sig in zip(entries, sigs, strict=True):
            hit = next((i for i, (old_sig, _) in enumerate(spare) if old_sig == sig), None)
            fresh.append(spare.pop(hit)[1] if hit is not None else make_providers([entry])[0])
        if not fresh:  # K3CODE_FAKE_PROVIDER still gives a chain without entries one scripted provider
            fresh = make_providers(entries)
        kept = {id(p) for p in fresh}
        self._retire_providers([p for p in self.providers if id(p) not in kept])
        self.providers, self._provider_sigs = fresh, sigs

    #: A replaced provider is closed once no turn is in flight (after PROVIDER_DRAIN_S at most); polled this often.
    PROVIDER_DRAIN_S = 900.0
    PROVIDER_DRAIN_POLL_S = 2.0

    def _retire_providers(self, providers: list[Any]) -> None:
        """Close replaced providers in the background once the turns that may hold them are done.

        Their httpx pools / temp dirs would otherwise wait for GC; but a turn in flight still calls them through the
        router it started with, so the close waits for the sessions to go quiet (``PROVIDER_DRAIN_S`` at most).
        """
        if not providers:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._close_when_drained(providers))
        except RuntimeError:  # no running loop (sync setup in tests): nothing to await it on
            return
        self._retiring.update({id(p): p for p in providers})
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    async def _close_when_drained(self, providers: list[Any]) -> None:
        deadline = time.monotonic() + self.PROVIDER_DRAIN_S
        while not self.stopping and time.monotonic() < deadline and any(s.turn_in_flight for s in self.live.values()):
            await asyncio.sleep(self.PROVIDER_DRAIN_POLL_S)
        await self._close_retired(providers)

    async def _close_retired(self, providers: list[Any] | None = None) -> None:
        """Close retired providers (all of them by default); one that is already closed is skipped."""
        for p in list(self._retiring.values()) if providers is None else providers:
            if self._retiring.pop(id(p), None) is None:
                continue
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001 - one provider failing to close must not keep the others open
                logger.warning("closing retired provider %s failed", getattr(p, "name", "?"), exc_info=True)

    _chain_key: str | None = None
    _router_cfg: Any = None  # the config.providers object the cached providers/routers were built from

    def reset_tier_routers(self) -> None:
        """Rebuild the routers on next use, over the same providers (task tiers / router options changed live)."""
        self._router_cache = {}
        if self._router_cfg is not None:  # built here; a router injected from outside (tests) stays as is
            self.router = None
            self._tiers = None

    def tier_routers(self) -> TierRouters:
        """The per-tier routers (built with the main router; rebuilt when the model key changes)."""
        if self._tiers is None:
            if self.router is not None:  # a router injected from outside (tests): every tier shares it
                self._tiers = TierRouters([], self.config, cooldowns=self.cooldowns, fallback_router=self.router)
            else:
                self._ensure_router()
        assert self._tiers is not None
        return self._tiers

    def _ensure_reliability(self, session: LiveSession) -> Reliability:
        """The session's Reliability bundle, built on first use (not started: see _reliability_for)."""
        if session.reliability is None:
            rel = build_reliability(self.config, session=session.session_id, home=self._home())
            rel.price_of = lambda usage: estimate_usd(self.config, usage)  # the USD budgets use real prices
            rel.events.add(lambda e: self._on_reliability_event(session, e), key="gateway")
            session.reliability = rel
        return session.reliability

    async def _reliability_for(self, session: LiveSession) -> Reliability:
        """One Reliability bundle per session: netwatch runs while the session lives, events go to its clients."""
        self._ensure_reliability(session)
        assert self.router is not None
        session.reliability.register_providers(self.router.chain)
        try:
            await session.reliability.start()
        except Exception as e:  # noqa: BLE001 - the turn still runs, without offline pause/resume
            logger.exception("session %s: NetWatch did not start; offline protection is off", session.session_id)
            if not session.offline_protection_error:  # one notice per session, not one per turn
                session.emit(
                    "notification.show",
                    {
                        "text": f"offline protection off: network watch failed to start ({e})",
                        "level": "warning",
                        "kind": "reliability",
                        "key": self.NETWATCH_KEY,
                    },
                    importance="essential",
                )
            session.offline_protection_error = str(e) or type(e).__name__
        else:
            if session.offline_protection_error:
                session.offline_protection_error = ""
                session.emit("notification.clear", {"key": self.NETWATCH_KEY}, importance="essential")
        return session.reliability

    def _on_router_event(self, event: RouterEvent) -> None:
        """Router events: failover/retry/exhausted → TUI + usage rows. Reliability kinds go via the session sink."""
        sess = _ctx_session.get()
        sid = sess.session_id if sess else ""
        if event.kind == "router.attempt":
            self.last_attempt = (event.provider, event.model)
            if (served := attempt_holder.get()) is not None:  # the ModelCaller call in flight in this context
                served.append((event.provider, event.model))
            if sess is not None:
                sess.last_entry = (event.provider, event.model)
                sess.last_tier = str(event.extra.get("tier", "main"))
                # Retries and failovers of one call count towards its time. Only a turn's own model calls are timed: a
                # side call after the turn (goal judge, auto-title) has no `call` row to close it, so its start would
                # stay armed and the next prompt's first row would include that call and the user's idle time.
                if sess.streaming and sess.call_started is None:
                    sess.call_started = time.monotonic()
        elif event.kind == "router.retry":
            self.usage.record("retry", session=sid, provider=event.provider, model=event.model, detail=event.reason)
        elif event.kind == "router.failover":
            self.usage.record("failover", session=sid, provider=event.provider, model=event.model, detail=event.reason)
            self.emit(
                "status.update",
                {
                    "kind": "status",
                    "text": f"failover: {event.provider}/{event.model} ({event.reason})",
                },
            )
        elif event.kind == "router.exhausted":
            if sess is not None:
                sess.call_started = None  # the failed call produces no row; the next one starts afresh
            # No error event here: the failed turn's message.complete reports it once, with the next step
            # (error_surface). A walk that is retried after a pause or park must not leave a stale error behind.
            self.emit("status.update", {"kind": "status", "text": f"all providers failed ({event.reason})"})

    #: Notification key shared by pause/park toasts so ``resumed`` can clear them.
    PAUSE_KEY: str = "k3.reliability.pause"  # annotated: the bare assignment reads as an API key to gitleaks
    #: Notification key of the "offline protection off" warning (NetWatch failed to start).
    NETWATCH_KEY = "k3.reliability.netwatch"

    def _on_reliability_event(self, session: LiveSession, event: Any) -> None:
        """Map a ``reliability.*`` / ``net.state`` loop event onto the TUI events that display it.

        Every event emitted here is ``importance: essential`` so focus mode keeps it.
        """
        kind: str = event.kind
        detail: str = event.detail
        data: dict[str, Any] = dict(event.data)
        ess = "essential"
        # Raw forward (the TUI ignores types it has no handler for).
        session.emit(kind, {"detail": detail, **data}, importance=ess)
        if kind in (rev.PAUSED, rev.PARKED):
            if kind == rev.PARKED:
                stamp = data.get("until") or time.time() + float(data.get("delay") or 0)
                until = time.strftime("%H:%M", time.localtime(float(stamp)))
                what = "all providers rate-limited" if "rate-limited" in detail else "waiting for provider"
                text = f"⏸ {what} until {until}"
            else:
                text = "⏸ offline — will resume automatically"
            session.paused = True
            session.paused_since = time.monotonic()
            session.pause_text = text
            session.emit("status.update", {"kind": "status", "text": text, "state": session.state}, importance=ess)
            session.emit(
                "notification.show",
                {"text": text, "level": "warning", "kind": "reliability", "key": self.PAUSE_KEY},
                importance=ess,
            )
        elif kind in (rev.RESUMED, rev.UNPARKED):
            if session.paused:
                secs = time.monotonic() - session.paused_since
                self.usage.record("pause", session=session.session_id, seconds=secs, detail=session.pause_text)
            session.paused = False
            session.pause_text = ""
            session.emit("notification.clear", {"key": self.PAUSE_KEY}, importance=ess)
            session.emit(
                "status.update", {"kind": "status", "text": "thinking", "state": session.state}, importance=ess
            )
        elif kind == rev.INTERRUPTED_TOOL:
            session.emit(
                "notification.show",
                {"text": f"Tool interrupted by a restart: {detail}", "level": "warning", "kind": "reliability"},
                importance=ess,
            )
        elif kind in (rev.BUDGET_EXCEEDED, rev.LOOP_DETECTED):
            session.needs_input = True
            if kind == rev.LOOP_DETECTED:
                session.loop_repeat = detail  # the guard's key for what repeated: "tool:<name>:<args>" or "text:..."
            label = "Budget exceeded" if kind == rev.BUDGET_EXCEEDED else "Loop detected"
            session.emit("error", {"message": f"{label}: {detail}"}, importance=ess)
        elif kind == rev.NEEDS_INPUT:
            session.needs_input = True

    # ── turn lifecycle ────────────────────────────────────────────────

    async def _run_turn(self, session: LiveSession, text: str, *, drain: bool = True) -> tuple[str, str]:
        """One user prompt, then (while a /goal is active) judge + auto-continue until done/paused/budget.

        ``drain=False`` leaves prompts queued behind this turn for the caller: an unattended tick must not answer the
        user's own prompt with its background flag, cheap tier and extra tools (see ServerRunner.run_prompt)."""
        async with session.turn_lock:  # a second turn on this session waits instead of interleaving with the first
            try:
                result = await self._run_turn_locked(session, text)
                # A prompt typed mid-turn used to be answered "queued" and then dropped. Run those now, in order.
                # halted: keep the queued prompts for after /daemon resume
                while (
                    drain and (pending := self._pending_prompts(session)) and result[0] not in ("interrupted", "halted")
                ):
                    result = await self._run_turn_locked(session, pending.pop(0))
                return result
            finally:
                session.goal_continuation = False

    @staticmethod
    def _pending_prompts(session: LiveSession) -> list[str]:
        """Queued prompts, steering messages no loop took first (they were typed earlier)."""
        if session.steer_queue:
            session.pending_prompts[:0] = _take_all(session.steer_queue)
        return session.pending_prompts

    def _kick_mcp(self) -> None:
        """Start the MCP servers now if they are not running. The turn needs them before its first model call; it
        used to start them there, after the user's hooks and the classifier (a slow stdio server added ~2 s).
        ``ensure_started`` is single-flight, so the turn's own call joins this one."""
        if self.halted or not self.mcp.cold:
            return
        task = asyncio.ensure_future(self.mcp.start_early())
        self._bg_tasks.add(task)  # held, so it is not collected mid-start and close() can cancel it
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # the turn's own call reports a failure

    async def _run_turn_locked(self, session: LiveSession, text: str) -> tuple[str, str]:
        _ctx_session.set(session)  # this task's events belong to the session, not to the requesting client
        effort_mod.REASONING_EFFORT.set(session.reasoning_effort)  # /effort, read by the providers
        typed = isinstance(text, TypedPrompt)
        pasted = text.paste_spans if isinstance(text, TypedPrompt) else ()
        prompt = text = str(text)  # the marker only says who wrote it (and which parts were pasted)
        mgr = self.goal_manager(session)
        self._kick_mcp()  # the servers start while the hooks and the scope classifier run
        # the prompt the user sent is not a continuation: clear the flag first, a turn that ended after a continuation
        # leaves it set and a queued prompt drained next would get no implicit goal
        session.goal_continuation = False
        session.goal_loop_streak = 0  # the user's own prompt starts the count of repeated loops again
        hooked: userhooks.HookOutcome | None = None  # the hooks' verdict on the typed prompt, handed to its first turn
        blocked = False  # a hook refused the prompt: it ends as a blocked turn, with no model or judge call around it
        if typed and not self.halted:
            # the user's hooks see the prompt before the scope classifier or a job does: one a hook blocks reaches no
            # model and no pipeline (it ends as a blocked normal turn below), and a hook's context goes into the job
            hooked = await self._typed_prompt_hooks(session, text)
            blocked = hooked.blocked
            if not blocked:
                # a wake word or the ultracode mode may run the prompt as a job instead of a normal turn
                routed = await self._route_typed_prompt(session, text, hooked, pasted)
                if routed is not None:
                    return routed
        if not blocked:  # a prompt a hook refused is not a task to carry on with
            self._start_implicit_goal(session, mgr, text)
        while True:
            # `!cmd` records made while the last turn ran go in before this one: its history (and n_before) has them
            self._flush_shell_records(session, force=True)
            try:
                if not blocked:
                    await self._maybe_compact(session)  # a blocked prompt reaches no model, not even the summary one
                prompt = self._with_long_goal(session, mgr, prompt)
                n_before = len(session.stored.messages)
                if hooked is None:
                    status, final_text = await self._run_one_turn(session, prompt)
                else:  # the typed prompt's hooks ran above; a retry or a goal's next prompt runs its own
                    status, final_text = await self._run_one_turn(session, prompt, hooked=hooked)
                    hooked = None
                pin: dict[str, Any] | None = None  # the turn's prompt: a fold that reaches into the turn keeps it
                folds = carried = 0  # compactions in this turn; model calls made before them (turns_carried)
                while (
                    status == "error"
                    and isinstance(session.last_exc, ContextOverflow)
                    and self.autocompact_policy(session).enabled  # /autocompact off: the user compacts, with /compact
                    and folds < MAX_OVERFLOW_FOLDS
                ):
                    # The provider says the conversation does not fit: fold the older history (and the earlier steps of
                    # this turn when it is long) into a summary, and go on; a turn that goes on long enough folds again.
                    failed_attempt = session.stored.messages
                    ran = failed_attempt[n_before:]
                    ran_tools = any(m.get("role") == "tool" for m in ran)
                    if pin is None:
                        pin = next((m for m in ran if m.get("role") == "user"), None)
                    # a further fold needs the steps the last retry made (else it overflowed at its first call)
                    if (folds and not ran_tools) or not await self._fold_after_overflow(
                        session, failed_attempt, n_before, pin
                    ):
                        # nothing could be folded: no retry, and the failed attempt stays in the history
                        session.stored.messages = failed_attempt
                        break
                    folds += 1
                    prompt = self._with_long_goal(session, mgr, prompt)  # the compaction may have folded the goal
                    if not ran_tools:
                        status, final_text = await self._run_one_turn(session, prompt, reuse_gate=session.turn_gate)
                        break
                    # tools already ran in this turn: keep that work and go on from it. Running the prompt again threw
                    # away every model call and replayed non-idempotent steps (a push, a migration)
                    n_before = len(session.stored.messages)  # a further overflow folds what this retry makes
                    carried += sum(1 for m in ran if m["role"] == "assistant")
                    session.turns_carried = carried
                    try:
                        status, final_text = await self._run_one_turn(
                            session, prompt, continuation=COMPACTED_CONTINUATION
                        )
                    finally:
                        session.turns_carried = 0
                if status == "error" and isinstance(session.last_exc, ContextOverflow):
                    self._clip_unfittable_prompt(session)
            except asyncio.CancelledError:
                # /stop and Esc cancel the turn task: the cancellation skips _run_one_turn's outcome mapping, so the
                # turn read 'idle' (or an earlier turn's 'failed'). Interrupted ends as completed, like TurnCancelled.
                session.run_result = "completed"
                self._block_goal_for(session, "interrupted")
                raise
            if blocked and status == "done":  # nothing was attempted for the goal: no judge call, no continuation
                self._session_finished(session, status)
                return status, final_text
            if self.halted and mgr.is_active() and status == "done":
                status = "halted"  # the halt arrived while the turn ran: no judge call, no continuation
            retry = None
            if status != "done" and not blocked and mgr.is_active():
                retry = self._goal_recovery(session, mgr, status)
            if retry:
                # a persistent goal does not pause on a failed or stalled turn: it waits a little (longer each time)
                # and goes on, until it is done or the user stops it
                self._drop_unanswered_continuation(session, prompt)
                delay, prompt, notice = retry
                session.emit("notification.show", {"text": notice, "level": "info", "kind": "info", "key": "goal"})
                session.emit(
                    "status.update", {"kind": "status", "text": f"goal retries in {delay:.0f} s", "state": "working"}
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    session.run_result = "completed"
                    self._block_goal_for(session, "interrupted")
                    raise
                if mgr.is_active() and not self.halted:
                    session.goal_continuation = True
                    continue
                status = "halted" if self.halted and mgr.is_active() else status
            if status == "done":
                session.goal_retry_streak = session.goal_loop_streak = 0
            if status != "done" or not mgr.is_active():
                self._block_goal_for(session, status)  # an active goal never ends silently
                self._session_finished(session, status)
                return status, final_text
            implicit = bool((state := mgr.state) and state.implicit)
            if implicit and self._pending_prompts(session):
                mgr.clear()  # the user typed the next prompt meanwhile: it replaces this one, unjudged
                self.emit_goal(session)
                self._session_finished(session, status)
                return status, final_text
            judge = self.goal_judge or make_judge(self._goal_completer(session))
            # no strong-tier advisor veto on an implicit goal: it would review every answered question
            reviewer = None if implicit else self._goal_reviewer(session)
            decision = await mgr.evaluate_after_turn(
                final_text,
                judge,
                cwd=session.stored.cwd or None,
                reviewer=reviewer,
                used_tools=session.last_tool_calls is None or session.last_tool_calls > 0,
            )
            self.emit_goal(session)
            if decision.message:
                session.emit(
                    "notification.show", {"text": decision.message, "level": "info", "kind": "info", "key": "goal"}
                )
            if not decision.should_continue or not decision.prompt:
                self._session_finished(session, status)
                return status, final_text
            prompt = decision.prompt
            session.goal_continuation = True  # unattended from here on: the goal drives the next turn

    def _flush_shell_records(self, session: LiveSession, *, force: bool = False) -> bool:
        """Put the session's pending `!cmd` records at the end of its transcript and save it; True when they went in.
        Never while a turn runs (its persist and checkpoints rewrite the list from the loop's messages, so they would
        be lost) unless ``force``: only the top of _run_turn_locked's loop, before the turn's history is read."""
        if not session.shell_records or (session.turn_in_flight and not force):
            return False
        records, session.shell_records = session.shell_records, []
        session.stored.messages = [*session.stored.messages, *records]
        self.store.save(session.stored)
        return True

    async def _fold_after_overflow(
        self, session: LiveSession, failed_attempt: list[dict[str, Any]], n_before: int, pin: dict[str, Any] | None
    ) -> bool:
        """Compact the conversation of a turn the provider rejected as too long; True when the turn can go on.

        ``failed_attempt``: the stored messages, the turn's own (from ``n_before`` on: its prompt, ``pin``, and the
        calls and results of the steps it made) among them. When tools ran, the whole conversation is folded: a long
        single turn (one prompt, hundreds of tool calls) leaves the older history next to nothing to fold, and a fold
        of that alone cost a summary and left the retry as long as it was. compact_messages cuts where most is freed,
        the turn's prompt and last steps stay whole. On True the stored messages are what the retry starts from."""
        ran_tools = any(m.get("role") == "tool" for m in failed_attempt[n_before:])
        half = context_window(self.config, self._compaction_model(session)) // 2
        session.stored.messages = failed_attempt if ran_tools else failed_attempt[:n_before]
        folded = 0
        try:
            for _ in range(2):  # the second only when the first (a cut at the turn's prompt) left the request long
                if not (done := await self._maybe_compact(session, force=True, pin=pin)):
                    break
                folded += done
                if not ran_tools or self._request_tokens(session) <= half:
                    break
        except BaseException:  # /stop during the summary call: the turn's prompt and tool work stay
            session.stored.messages = failed_attempt
            raise
        return bool(folded)

    @staticmethod
    def _with_long_goal(session: LiveSession, mgr: GoalManager, prompt: str) -> str:
        """A goal continuation quotes a long goal by its start and a pointer to the message that holds it (see
        goals.quote). When no message of the retained history holds the whole text any more (a compaction folded it, the
        first-message anchor cut it, /clear), the prompt carries the whole goal again."""
        state = mgr.state
        if state is None or len(state.goal) <= GOAL_QUOTE_CHARS or quote(state.goal) not in prompt:
            return prompt
        if any(state.goal in str(m.get("content") or "") for m in session.stored.messages):
            return prompt
        return prompt.replace(quote(state.goal), state.goal)

    def _start_implicit_goal(self, session: LiveSession, mgr: GoalManager, text: str) -> None:
        """``autonomy.auto_continue``: an interactive prompt becomes the goal of its own turn loop, judged after each
        turn like a /goal, so a model that stops early is sent on until the judge says done or blocked.

        Only an attended prompt the user typed qualifies: background, cron, loop and automation turns
        (``session.background``/``task_kind``) and goal continuations do not. A /goal, active or paused, is the
        user's own and wins: a paused one waits for /goal resume, and the answer typed meanwhile must not erase it.
        A leftover implicit goal (a daemon that died mid-loop) is replaced, or cleared with the setting off.
        """
        if session.background or session.task_kind or session.goal_continuation:
            return
        state = mgr.state
        if state is not None and not state.implicit and state.status in ("active", "paused"):
            return
        if autonomy_cfg(self.config).get("auto_continue"):
            mgr.set(text, implicit=True)
        elif state is not None and state.implicit:
            mgr.clear()
        else:
            return
        self.emit_goal(session)

    # ── wake words and the ultracode mode ──

    async def _route_typed_prompt(
        self,
        session: LiveSession,
        text: str,
        hooked: userhooks.HookOutcome,
        pasted: tuple[tuple[int, int], ...] = (),
    ) -> tuple[str, str] | None:
        """A prompt the user typed: a wake word, else the ultracode mode, runs a job as this turn.

        ``hooked``: what the user's hooks made of it (not blocked: the caller ends a blocked prompt as a normal turn).
        ``pasted``: offsets of the pasted parts, where a wake word does not count. Returns the job's ``(status, text)``;
        None means run ``text`` as a normal turn. Deciding never costs the user their prompt: a failure while deciding
        is logged and the prompt runs normally."""
        try:
            route = await self._plan_route(session, text, hooked.context_text(), pasted)
        except asyncio.CancelledError:  # /stop during the scope check: the ending of a cancelled turn
            session.run_result = "completed"
            self._block_goal_for(session, "interrupted")
            session.emit("status.update", {"kind": "status", "text": "", "state": session.state})
            raise
        except Exception:  # noqa: BLE001
            logger.exception("wake word / ultracode routing failed; running the prompt as a normal turn")
            return None
        if route is None:
            return None
        if isinstance(route.spec, dict):  # usage line, or the mode is unavailable: an answer, no job
            status, out = self._reply_turn(session, text, str(route.spec["message"]))
        else:
            spec = route.spec

            async def run() -> str:
                self.ultra.progress(session, route.command, route.phase, route.detail)
                return await spec.factory()

            # the user's own words are the user message; the label only names the job in the status line
            status, out = await self._run_job(session, spec.label, run, user_text=text)
        self._block_goal_for(session, status)
        self._session_finished(session, status)
        return status, out

    async def _plan_route(
        self, session: LiveSession, text: str, context: str = "", pasted: tuple[tuple[int, int], ...] = ()
    ) -> _Route | None:
        """Which job (if any) ``text`` runs as: an explicit wake word first, then the ultracode mode.

        ``context``: what the user's UserPromptSubmit hooks added, handed to the job with the task. ``pasted``: offsets
        of pasted text, where a wake word does not count."""
        if (hit := wakewords.detect_enabled(self.config, text, pasted)) is not None:
            cmd: Any = self.commands.get(hit.mode)
            if hit.mode == "ultracode" and (word := typed_mode_word(hit.task)):
                # "ultracode off", "turn ultracode off": what /ultracode takes as mode control, not a task to run
                return _Route(hit.mode, "wake word", "", cmd.apply_mode_word(self, session, word))
            # an empty task (the word alone) comes back as the command's usage line
            spec = await cmd.prepare(self, session, hit.task, context=context)
            return _Route(hit.mode, "wake word", f'"{hit.mode}" in your message', spec)
        if (
            getattr(session, "ultra_mode", "off") != "ultracode"
            or session.background  # unattended runs have nobody to watch a pipeline
            or session.preapproved_plan  # /go after /ultraplan: the plan is approved, the gate hands it to fan-out
            or text.lstrip().startswith("/")
        ):
            return None
        if dirty := await dirty_checkout(Path(session.perms.cwd)):
            # /ultracode refuses a checkout with uncommitted changes (the fan-out merges into it): the prompts of the
            # always-on mode get an ordinary turn until it is committed (checked before the scope verdict, which
            # would cost a classifier call for nothing), and the user hears it once per dirty spell, for 15 s
            if not session.ultra_dirty_noted:
                session.ultra_dirty_noted = True
                session.emit(
                    "notification.show",
                    {
                        "text": f"ultracode is on but not running: {dirty_text(dirty)}. Prompts get an ordinary turn.",
                        "level": "info",
                        "kind": "ttl",
                        "ttl_ms": 15000,
                        "key": f"k3.ultra_dirty.{session.session_id}",
                    },
                )
            return None
        session.ultra_dirty_noted = False  # clean again: the next dirty spell is announced anew
        session.emit("status.update", {"kind": "status", "text": "checking scope", "state": "working"})
        verdict = await self.autonomy.mode_verdict(session, text)
        min_scope = str(ultra_cfg(self.config)["min_scope"])
        if verdict.source == "fallback" or SCOPES.index(verdict.scope) < SCOPES.index(min_scope):
            return None  # trivial, or the classifier is down: not worth a pipeline; the gate reuses this verdict
        ultracode: Any = self.commands.get("ultracode")
        spec = await ultracode.prepare(self, session, text, context=context, verdict=verdict)
        self.autonomy.drop_mode_verdict(session)
        return _Route("ultracode", "ultracode is on", f"scope {verdict.scope}", spec)

    @staticmethod
    def _drop_unanswered_continuation(session: LiveSession, prompt: str) -> None:
        """A goal's continuation turn that failed before the model answered leaves its prompt as the last message of
        the history; the retry sends a new one. Each failed retry used to add another (17 an hour while a provider was
        down), all resent with every later request. The user's own prompt is never dropped."""
        messages = session.stored.messages
        if not session.goal_continuation or not messages:
            return
        last = messages[-1]
        if last.get("role") == "user" and str(last.get("content") or "").startswith(prompt):
            session.stored.messages = messages[:-1]

    @staticmethod
    def _loop_call(key: str) -> str:
        """What the loop guard stopped on, from its key (``tool:<name>:<args json>`` or ``text:<message>``)."""
        kind, _, rest = key.partition(":")
        if kind != "tool":
            return "the same message"
        name, _, args = rest.partition(":")
        return f"the call `{name}` ({scrub_text(args)[:80]})"

    def _goal_recovery(self, session: LiveSession, mgr: GoalManager, status: str) -> tuple[float, str, str] | None:
        """What a persistent goal does when its turn did not finish: ``(seconds to wait, next prompt, notice)``, or None
        to leave it to ``_block_goal_for`` (pause). Persisting covers a failed turn (no provider answered after the
        retries, an exception) and one that stopped for input (a question, an approval nobody answered, the loop
        guard). It does not cover what the user set or did: a spend budget, ``/stop``, ``/daemon pause``."""
        state = mgr.state
        if not mgr.persist or state is None or state.implicit:
            return None
        if status == "error":
            why = f"it failed ({scrub_text(session.last_error or 'see the error above')[:160]})"
        elif status == "needs_input" and not session.budget_stopped:
            why = "it stopped for input (a question, an approval nobody answered, or the loop guard)"
            if session.loop_repeat:
                # the same call again and again: a few more tries, then the goal pauses (see _block_goal_for); a
                # single stop is retried as before
                session.goal_loop_streak += 1
                if session.goal_loop_streak > MAX_LOOP_RECOVERIES:
                    return None
                call = self._loop_call(session.loop_repeat)
                why = f"the loop guard stopped it for repeating {call}; do not repeat it"
        else:
            return None
        if not mgr.count_recovery_turn():
            return None  # the user's --turns budget is spent: the goal pauses
        session.goal_retry_streak += 1
        delay = GoalManager.backoff(session.goal_retry_streak - 1)
        prompt = CONTINUATION_PROMPT_RECOVER_TEMPLATE.format(goal=quote(state.goal), why=why)
        notice = f"↻ The goal's turn did not finish: {why}. Trying again in {delay:.0f} s; /goal clear stops it."
        return delay, prompt, notice

    def _block_goal_for(self, session: LiveSession, status: str) -> None:
        """A turn that did not finish leaves its active goal paused, with the reason and one notification.

        ``needs_input`` (budget, loop guard, a question), ``interrupted`` (/stop, a graceful stop), ``halted``
        (/daemon pause) and ``error`` each get their own pause reason, so nothing waits in the background silently.
        """
        mgr = self.goal_manager(session)
        if not mgr.is_active():
            return
        if (state := mgr.state) is not None and state.implicit:
            mgr.clear()  # an implicit goal ends with its turn: no pause, no "/goal resume" notice (GoalState.implicit)
            self.emit_goal(session)
            return
        if status == "needs_input" and session.goal_loop_streak > MAX_LOOP_RECOVERIES and session.loop_repeat:
            reason = "needs_input"
            text = (
                f"Goal paused: the model kept repeating {self._loop_call(session.loop_repeat)} through "
                f"{MAX_LOOP_RECOVERIES} retries and the loop guard stopped it each time. Change the goal or the "
                "approach, then /goal resume."
            )
            session.goal_loop_streak = 0  # /goal resume gets a fresh set of retries
        elif status == "needs_input" and session.budget_stopped:
            # a spend cap (reliability.*) or the max_turns cap stopped the turn: nobody has a question to answer
            detail = scrub_text(session.last_error)[:160]
            reason = "needs_input"
            text = (
                f"Goal paused: a spend cap stopped the turn ({detail}). Raise it (reliability.* in the config), or "
                "/goal resume after it resets (a day cap resets at midnight)."
                if detail
                else "Goal paused: the max_turns cap on model calls per prompt stopped the turn. Raise max_turns "
                "in the config, then /goal resume."
            )
        elif status == "needs_input":
            reason, text = "needs_input", "Goal needs your input before it can continue. Answer, then /goal resume."
        elif status == "interrupted" and self.stopping:
            reason, text = "daemon restart", "Goal stopped by the daemon restart; it resumes on the next boot."
        elif status == "interrupted":
            reason, text = "interrupted", "Goal interrupted. /goal resume to continue."
        elif status == "halted":
            reason, text = "halted", "Goal paused: the daemon is halted (/daemon pause). /goal resume after resume."
        elif status == "error" and isinstance(session.last_exc, (ChainExhausted, AllProvidersUnreachable)):
            detail = scrub_text(session.last_error or "no provider answered")[:160]
            reason = "provider unavailable"
            text = f"Goal paused: no provider answered after the retries ({detail}). /goal resume to retry."
        elif status == "error":
            detail = scrub_text(session.last_error or "see the error above")[:160]
            reason, text = "turn failed", f"Goal paused: the turn failed ({detail}). /goal resume to retry."
        else:
            return
        mgr.pause(reason)
        self.emit_goal(session)
        self.notify_blocker(
            session.session_id, text, level="warning", kind="goal", key=f"k3.goal.blocked.{session.session_id}"
        )

    def _session_finished(self, session: LiveSession, status: str) -> None:
        """Tell the automation engine (``session_event`` triggers) that a session's run ended."""
        if self.automation is not None:
            self.automation.session_event(session.session_id, status, str(session.stored.meta.get("origin") or ""))

    def _finished_background_note(self, session: LiveSession) -> tuple[str, list[Any]]:
        """The background sub-agents (``task(background=true)``) that finished since the model last looked, one line
        each (and the handles they came from). Their output was only ever read by a ``task_result`` call the model had
        to remember to make; their spend was already counted. The handles are marked reported once the model has
        answered the prompt that carries the note: a prompt dropped by an overflow rerun or a goal retry never
        reached it."""
        done = self.subagents.unreported(session.session_id)
        if not done:
            return "", []

        def outcome(h: Any) -> str:
            text = h.error if h.status == "failed" and h.error else h.result or h.error
            return text.strip()[:200]

        lines = [f'- {h.id} ({h.status}): {outcome(h)}  [task_result(id="{h.id}")]' for h in done]
        head = "[Background sub-agents finished since your last turn; read the full result with task_result]"
        return head + "\n" + "\n".join(lines), done

    def _charge_side_call(self, session_id: str, usage: Usage) -> None:
        """A one-shot call's usage (see ModelCaller) counts against the session's budgets and the day's ledger."""
        live = self.live.get(session_id)
        if live is not None:  # the bundle may not exist yet: a resumed session's compaction runs before its first turn
            self._ensure_reliability(live).record_usage(usage)

    def _system_prompt_for(self, session: LiveSession) -> str:
        """The session's system prompt, with the learned notes as the session first saw them."""
        if session.learned_notes is None:
            session.learned_notes = read_learned(session.perms.cwd)
        return build_system_prompt(
            session.system_prompt,
            cwd=session.perms.cwd,
            config=self.config,
            session_meta=session.stored.meta,
            mcp=self.mcp,
            learned=session.learned_notes,
            frozen=session.prompt_frozen,
        )

    def _build_loop(
        self,
        session: LiveSession,
        reliability: Reliability,
        router: Router,
        kind: TaskKind,
        approval: Any,
        *,
        max_tool_errors: int = 0,
        escalates: bool = False,
    ) -> AgentLoop:
        """``escalates``: a cheap/fast attempt that a higher tier continues when it stalls, so a tool-error stop is
        silent (the next tier carries on); otherwise the stop ends the turn with the list of failed calls."""
        loop = AgentLoop(
            router,
            system_prompt=self._system_prompt_for(session),
            max_turns=self.config.max_turns,
            turns_carried=session.turns_carried,
            headless=False,
            on_event=self._on_router_event,
            cwd=session.perms.cwd,
            approval_callback=approval,
            plan_callback=self._plan_callback_for(session),
            on_auto_allow=lambda tool, args, dec: session.emit(
                "permission.auto_allowed",
                {"session_id": session.session_id, "tool": tool, "command": _command_for_tool(tool, args)},
            ),
            permissions=session.perms,
            reliability=reliability,
            session=session.session_id,
            background=session.background,
            unattended=session.goal_continuation,
            unattended_network=bool(autonomy_cfg(self.config)["unattended_network"]),
            task_kind=kind.value,
            max_tool_errors=max_tool_errors,
            tool_output_chars=tool_output_limit(self.config),
            context_window=self._router_window(router, session),
        )
        loop.share_elision(session.elided, session.elide_notes)
        loop.share_reads(session.reads, session.pinned_reads)
        loop.on_checkpoint = lambda: self._checkpoint_turn(session)  # prompt + tool call hit the disk before the tool
        loop.take_steer = lambda: _take_all(session.steer_queue)
        loop.file_stamps = session.file_stamps
        register_todo(loop.tools, session.stored.meta)  # the list lives in the session's meta and is saved with it
        self._register_advisor(loop, session)
        loop.hooks = userhooks.load(session.perms.cwd, session.session_id)  # re-read per loop: config edits apply
        loop.tool_error_stop_message = not escalates
        decision = decision_settings(self.config)
        if decision.enabled:
            loop.elide_at_ratio = decision.at_ratio
            loop.decide_context = lambda wire, candidates: decide(
                self.model_caller,
                wire,
                candidates,
                decision,
                session_id=session.session_id,
                router=self._decision_router(decision),
            )
        loop.on_tool_outcome = lambda call, result, failure: self.learning.tool_outcome(session, call, result, failure)
        register_skill_tool(loop.tools, session.perms.cwd, list(self.config.skills.roots))
        register_mcp_tools(loop.tools, self.mcp)
        # the MCP tools this session already loaded stay advertised, in the order it loaded them: a tool list that
        # shrinks back at every turn start is a prompt-cache miss for the whole request, twice per search
        loop.tools.activate(list(session.stored.meta.get("mcp_active") or []))
        for install in session.extra_tools:
            install(loop.tools)
        register_task_tools(loop.tools, self, session, depth=1)
        register_web_tools(loop.tools, self.config, fetcher=self.web_fetcher, mcp=self.mcp, browser=self.browser)
        session.overhead_tokens = overhead_tokens(loop.system_prompt, loop.tool_specs())
        return loop

    async def _prompt_hooks(
        self, session: LiveSession, hooks: userhooks.HookRunner | None, text: str
    ) -> userhooks.HookOutcome:
        """SessionStart (once per session in this process) and UserPromptSubmit; their context is merged."""
        outcome = userhooks.HookOutcome()
        if not hooks:
            return outcome
        if not session.hooks_started:
            session.hooks_started = True
            source = "resume" if session.stored.messages else "startup"
            started = await hooks.run("SessionStart", {"source": source})
            outcome.context += started.context
        submitted = await hooks.run("UserPromptSubmit", {"prompt": text})
        outcome.blocked, outcome.reason = submitted.blocked, submitted.reason
        outcome.context += submitted.context
        return outcome

    async def _typed_prompt_hooks(self, session: LiveSession, text: str) -> userhooks.HookOutcome:
        """The hooks for a prompt the user typed, run ahead of the turn so that nothing (the scope classifier, a
        wake word's job) spends a model call on a prompt a hook blocks; ``_run_one_turn`` takes the outcome as given."""
        hooks = userhooks.load(Path(session.stored.cwd or Path.cwd()), session.session_id)
        if hooks:  # a hook can take a while: the turn is working, as it was when the hooks ran inside it
            session.emit("status.update", {"kind": "status", "text": "running hooks", "state": "working"})
        try:
            return await self._prompt_hooks(session, hooks, text)
        except asyncio.CancelledError:  # /stop while a hook runs: the ending of a cancelled turn
            session.run_result = "completed"
            self._block_goal_for(session, "interrupted")
            session.emit("status.update", {"kind": "status", "text": "", "state": session.state})
            raise

    def _active_model(self, session: LiveSession) -> str:
        """The model id the session's main tier sends to first (what its context window is looked up by)."""
        specs = tier_model_specs(self.config, Tier.MAIN, key=session.stored.model or self.config.default_model)
        first = specs[0] if specs else ""
        if isinstance(first, list):
            first = first[0] if first else ""
        return str(first or session.stored.model or self.config.default_model)

    def _compaction_model(self, session: LiveSession) -> str:
        """The model id whose context window decides when the session is compacted: the smallest among the models its
        next turn can run on, that is the main tier's models on every provider of the chain (a failover lands on a
        later one) and, for a loop tick, cron job or background turn, those of the tier the kind runs on (the cheap
        one). Judged by the main tier's first model alone, a loop session overflowed a smaller cheap model."""
        tiers = {Tier.MAIN}
        if session.task_kind:
            tiers.add(tier_for(session.task_kind, self.config.task_tiers))
        models: list[str] = []
        for tier in tiers:
            for spec in tier_model_specs(self.config, tier, key=session.stored.model or self.config.default_model):
                models.extend(m for m in ([spec] if isinstance(spec, str) else spec) if m)
        if not models:
            return self._active_model(session)
        return min(models, key=lambda m: context_window(self.config, m))

    def _summary_window(self, session: LiveSession) -> int:
        """The smallest context window among the models the summary call can run on (the ``compaction`` kind's tier: the
        cheap one by default), which the summary's input is sized to: ``_compaction_model`` looks at the main tier."""
        tier = tier_for(TaskKind.COMPACTION, self.config.task_tiers)
        models: list[str] = []
        for spec in tier_model_specs(self.config, tier, key=session.stored.model or self.config.default_model):
            models.extend(m for m in ([spec] if isinstance(spec, str) else spec) if m)
        return min((context_window(self.config, m) for m in models), default=context_window(self.config, ""))

    def _decision_router(self, decision: Any) -> Router | None:
        """The router for ``context.decision_model.model`` (on its ``provider`` block, or on every block), or None
        when no model is named: the call then goes to the tier ``task_tiers.context_select`` picks (cheap)."""
        if not decision.model:
            return None
        providers = [p for p in self.providers if not decision.provider or p.name == decision.provider]
        if not providers:
            raise ValueError(f"context.decision_model.provider {decision.provider!r} names no provider block")
        return Router(
            build_chain(providers, [decision.model] * len(providers)),
            cooldowns=self.cooldowns,
            on_event=self._on_router_event,
            tier="cheap",
            **router_options(self.config),
        )

    def _clip_unfittable_prompt(self, session: LiveSession) -> None:
        """A last user message that alone overflows the window is clipped in the stored history. The model never saw it;
        left whole it was resent, and rejected, by every later prompt, and neither /compress nor /retry could help.
        One that fits the window (with the system prompt, the tools and a reply) is never touched: the overflow came
        from the history or the reply, and a clipped message loses what the user wrote."""
        last = session.stored.messages[-1] if session.stored.messages else {}
        content = last.get("content")
        if last.get("role") != "user" or not isinstance(content, str):
            return
        window = context_window(self.config, self._compaction_model(session))
        room = window - self._request_tokens(session, []) - min(self.config.max_tokens, window // 8)
        tokens = text_tokens(content)
        if tokens <= room:
            return
        limit = max(1000, len(content) * max(room // 2, 1) // tokens)  # chars: half of what the window leaves
        removed = len(content) - limit
        marker = f"\n…[{removed:,} characters removed: this message alone is larger than the context window]"
        session.emit(
            "notification.show",
            {
                "text": f"Your message is larger than the {window:,}-token context window: the last {removed:,} of its "
                f"{len(content):,} characters were cut",
                "level": "warning",
                "kind": "ttl",
                "ttl_ms": 15000,
                "key": f"k3.prompt_clipped.{session.session_id}",
            },
        )
        session.stored.messages[-1] = {**last, "content": content[:limit] + marker}
        self.store.save(session.stored)

    def _fits_tier(self, session: LiveSession, tier: Tier, prompt: str) -> bool:
        """Whether the session's next request (its conversation and ``prompt``) is within the share of the smallest
        window of ``tier`` at which a session is compacted. A trivial prompt starts on the cheap tier, whose models may
        take far less than the main one: a long history was sent to a 32k model and rejected."""
        router = self.tier_routers().get(tier)
        if router is None:
            return True
        window = self._router_window(router, session)
        return self._request_tokens(session) + text_tokens(prompt) <= int(window * COMPACT_AT_RATIO)

    def _router_window(self, router: Router, session: LiveSession) -> int:
        """The smallest context window among the models ``router`` can send to (a cheap tier or an escalated one
        has its own), so in-turn elision starts before the tightest model overflows."""
        return router_window(self.config, router, self._active_model(session))

    async def _run_one_turn(
        self,
        session: LiveSession,
        text: str,
        *,
        hooked: userhooks.HookOutcome | None = None,
        continuation: str | None = None,
        reuse_gate: GateResult | None = None,
    ) -> tuple[str, str]:
        """Execute one prompt end-to-end, emitting wire events. Returns (status, final_text).

        ``hooked``: the outcome of the user's hooks when the caller already ran them for ``text``. ``continuation``:
        the turn goes on after a compaction in its middle; ``text`` is already in the history, so the hooks and the
        scope gate (which ran for it) do not run again and the model is sent this note instead. ``reuse_gate``: the
        gate of an earlier attempt at ``text`` (kept in ``session.turn_gate``): the prompt runs again after an overflow
        with the verdict and the plan it had, not through a second classifier call, planning turn and approval."""
        if self.halted:  # /daemon pause: nothing reaches a provider; the caller pauses the goal (status 'halted')
            return "halted", ""
        self._ensure_router(session.stored.model or None)
        assert self.router is not None
        session.turn_id = uuid.uuid4().hex[:12]
        self.subagents.begin_turn(session.session_id)  # the per-turn count of the model's task calls starts again
        session.perms.cwd = Path(session.stored.cwd or Path.cwd())  # session cwd, never the process cwd
        session.perms.reload()
        session.needs_input = False
        session.budget_stopped = False
        session.run_result = None
        session.last_error, session.last_exc, session.last_api_calls = "", None, 0
        session.loop_repeat, session.last_tool_calls = "", None
        session.turn_gate = None
        reliability = await self._reliability_for(session)
        reliability.set_unattended(session.background)  # unattended: provider exhaustion parks instead of failing
        await self.mcp.ensure_started()
        approval = await self._approval_callback_for(session)

        # M4a: which tier runs this turn; cheap/fast tiers escalate when the attempt stalls.
        kind = (
            TaskKind(session.task_kind)
            if session.task_kind and session.background
            else (TaskKind.BACKGROUND_TURN if session.background else TaskKind.INTERACTIVE_TURN)
        )
        tier = tier_for(kind, self.config.task_tiers)
        cheap_start = tier in (Tier.FAST, Tier.CHEAP)
        # A turn that starts on main climbs once to strong when it stalls ("never stop until the task is done"); only
        # a stall there ends the turn with needs_input. ``autonomy.escalate_main: false`` keeps main as the last tier.
        main_climbs = tier is Tier.MAIN and bool(autonomy_cfg(self.config).get("escalate_main", True))
        main_errors = int(autonomy_cfg(self.config)["max_tool_errors"])  # a main-tier turn stops after this many
        max_errors = int(autonomy_cfg(self.config)["escalate"]["tool_errors"]) if cheap_start else main_errors
        escalation = Escalation(tier, thresholds={"tool_errors": 1, "loop_guard": 1})  # the loop counted already

        config = self.config
        final_text = ""
        text_in_flight = False  # a message.delta went out for the model call now streaming (see on_text_reset)
        usage = Usage()
        error: str | None = None
        status = "done"
        run_stop_hook = False  # set once the turn ran to its end without an exception
        cancelled = False  # set when the turn task is cancelled (/stop, Esc)
        gate = GateResult(prompt=text)
        # bound before the try: a /stop during the scope gate reached the finally first, whose replay record then
        # raised UnboundLocalError in place of the CancelledError
        history: list[Message] = []
        loop = self._build_loop(
            session,
            reliability,
            self.tier_routers().get(tier),
            kind,
            approval,
            max_tool_errors=max_errors,
            escalates=cheap_start or main_climbs,
        )
        session.loop = loop

        async def on_text_delta(chunk: str) -> None:
            nonlocal final_text, text_in_flight
            final_text += chunk
            text_in_flight = True
            session.emit("message.delta", {"text": chunk})

        async def on_text_reset() -> None:
            nonlocal final_text, text_in_flight
            final_text = ""  # a retried attempt streams the answer again; judge and loop hash want one copy
            if text_in_flight:
                # the client holds the partial text of the attempt that just failed: without this it shows it, then the
                # retry's copy after it ("PARTIAL PARTIAL FULL"), and the answer twice once the turn completes
                text_in_flight = False
                session.emit("message.reset", {})

        loop.on_text_delta = on_text_delta
        loop.on_text_reset = on_text_reset

        session.emit("message.start", {})
        session.emit("status.update", {"kind": "status", "text": "thinking", "state": "working"})
        session.streaming = True
        session.turn_started_wall = time.time()
        session.current_kind = kind.value
        prompt_blocked = False
        try:
            if continuation is not None:
                hooked = userhooks.HookOutcome()
            if hooked is None:
                hooked = await self._prompt_hooks(session, loop.hooks, text)  # before anything spends a model call
            prompt_blocked = hooked.blocked
            try:
                if not hooked.blocked and continuation is None:
                    if reuse_gate is not None:
                        gate = dataclasses.replace(reuse_gate)
                    else:
                        gate = await self.autonomy.prepare(session, text)  # M4a: scope gate + planning turn
                    session.turn_gate = dataclasses.replace(gate)  # as the gate decided it: a rerun after an overflow
            except Exception:  # noqa: BLE001 - the autonomy layer must never block the user's task
                logger.exception("autonomy gate failed; running the task directly")
            session.current_kind = kind.value
            acfg = autonomy_cfg(config)
            if (
                kind is TaskKind.INTERACTIVE_TURN
                and not cheap_start
                and tier is Tier.MAIN
                and (tier_for(kind, config.task_tiers) is Tier.MAIN)
                and acfg.get("degrade_trivial", True)
                and gate.proceed
                and gate.verdict is not None
                and gate.verdict.scope == "trivial"
                and self._fits_tier(session, Tier.CHEAP, gate.prompt)
            ):
                # A trivial task is "unimportant work": start it on the cheap tier. The loop escalates to main when the
                # attempt stalls (tool errors, loop guard), so a task the cheap model cannot do still gets done.
                tier, cheap_start, main_climbs = Tier.CHEAP, True, False
                max_errors = int(acfg["escalate"]["tool_errors"])
                escalation = Escalation(tier, thresholds={"tool_errors": 1, "loop_guard": 1})
                loop = self._build_loop(
                    session,
                    reliability,
                    self.tier_routers().get(tier),
                    kind,
                    approval,
                    max_tool_errors=max_errors,
                    escalates=True,
                )
                loop.on_text_delta = on_text_delta
                loop.on_text_reset = on_text_reset
                loop.on_checkpoint = lambda: self._checkpoint_turn(session)
                session.loop = loop
            history = session.history
            prompt = continuation if continuation is not None else gate.prompt
            if hooked.blocked:
                gate.proceed, gate.message = False, f"Prompt blocked by a UserPromptSubmit hook: {hooked.reason}"
            elif extra := hooked.context_text():
                prompt = f"{prompt}\n\n" + fenced("context from the user's hooks:", extra)
            noted: list[Any] = []  # background handles whose note this prompt carries, until the model answers it
            if not hooked.blocked:
                finished, noted = self._finished_background_note(session)
                if finished:
                    prompt = f"{prompt}\n\n{finished}"
            if gate.proceed and (subtasks := self.fanout.applies(session, gate)):
                fan = await self.fanout.run(session, text, gate.plan, subtasks)  # M4b: parallel worktree children
                if fan is not None and fan.ok:
                    gate.proceed, gate.message = False, fan.summary()
                    session.stored.messages = [
                        *session.stored.messages,
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": gate.message},
                    ]
                    self.store.save(session.stored)
                elif fan is not None:
                    prompt = fan.escalation_prompt(text)  # the parent finishes what the children could not
                    noted = []  # this prompt does not carry the note: it is shown on the next turn
            if not gate.proceed:
                final_text = gate.message
                session.emit("message.delta", {"text": gate.message})
            while gate.proceed:
                async for event in loop.run(
                    prompt,
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                    history=history,
                ):
                    self._on_stream_event(session, event)
                    # a reply the window cut short is dropped for an overflow rerun: the note it carried did not arrive
                    answered = (
                        event.type == "done"
                        and event.message is not None
                        and event.message.role == "assistant"
                        and event.message.stop_reason != "model_context_window_exceeded"
                    )
                    if answered:
                        text_in_flight = False  # that reply is complete: a later reset concerns the next call's text
                    if noted and answered:
                        for h in noted:
                            h.reported = True
                        noted = []
                attempt_reason = loop.escalation_reason or ""
                if "loop_guard" in attempt_reason:  # counted on every tier, not only when a cheap start escalates
                    self.usage.record(
                        "loop_guard",
                        session=session.session_id,
                        detail=attempt_reason,
                        tier=tier.value,
                        task_kind=kind.value,
                        turn=session.turn_id,
                    )
                climbs = cheap_start or (main_climbs and tier is Tier.MAIN)
                # the max_turns cap is the user's limit on the task, not a stall: no tier gets a fresh round of calls
                stalled = attempt_reason and attempt_reason != "max_turns"
                new_tier = escalation.record(attempt_reason) if climbs and stalled else None
                if new_tier is None and attempt_reason in ("tool_errors", "max_turns") and not loop.interrupted:
                    # the loop stopped after N failed calls in a row (and listed them) or at the configured max_turns
                    # cap (and said so): the user decides how to go on (ending 'done' let an active goal judge it and
                    # continue into the same failures, and a capped task looked finished)
                    session.needs_input = True
                    if attempt_reason == "max_turns":  # a limit the user set: a persistent goal does not retry past it
                        session.budget_stopped = True
                if new_tier is None or loop.interrupted:
                    break
                # The attempt stalled on a cheap tier (or on main, see main_climbs): continue the same task one tier up.
                reason = attempt_reason or "unknown"
                self.model_caller.note_escalation(kind, tier, new_tier, reason, session.session_id)
                tier = new_tier
                session.needs_input = False
                session.loop_repeat = ""  # a higher tier took over: a later stop is not this loop
                if reliability.loop_guard is not None:
                    reliability.loop_guard.reset()
                history = [m for m in loop.turn_messages if m.role != "system"]
                prompt = (
                    f"The previous attempt stalled ({reason}). Continue the task from where it left off, "
                    "with a different approach if needed."
                )
                loop = self._build_loop(
                    session,
                    reliability,
                    self.tier_routers().get(tier),
                    kind,
                    approval,
                    max_tool_errors=max_errors if tier in (Tier.FAST, Tier.CHEAP) else main_errors,
                    escalates=cheap_start and next_tier(tier) is not None,
                )
                loop.on_text_delta = on_text_delta
                loop.on_text_reset = on_text_reset
                session.loop = loop
            if session.needs_input:
                # The loop guard stopped the turn (the escalation above clears the flag when a higher tier takes
                # over). Ending it 'done' let an active goal judge it and continue into the same loop, and reported a
                # /loop tick as completed.
                status = "needs_input"
            run_stop_hook = bool(loop.hooks) and not hooked.blocked  # it runs once the client has the answer, below
        except (AllProvidersUnreachable, ChainExhausted, ContextOverflow, DiskGuardFull) as e:
            status = "error"
            error = str(e)
            session.last_exc = e  # message.complete carries it with error_surface; no separate error toast
        except BudgetExceeded as e:
            # The reliability event already told the client (error + needs_input).
            status = "needs_input"
            error = str(e)
            session.needs_input = True
            session.budget_stopped = True
        except TurnCancelled:
            status = "interrupted"
        except asyncio.CancelledError:
            status = "interrupted"
            cancelled = True
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("turn failed")
            status = "error"
            error = str(e)
            session.last_exc = e
            session.emit("error", {"message": str(e)})
        finally:
            session.streaming = False
            session.call_started = None  # a call cut short by /stop or an error has no row to close it
            session.idle_since = time.monotonic()
            # Persist whatever the loop accumulated, also on error and on /stop or shutdown (CancelledError):
            # this used to sit after the try block, which a cancellation skipped, so the whole turn vanished.
            await self._persist_turn(session, loop)
            self.learning.record_turn_replay(
                session,
                list(loop.turn_messages),
                status,
                tier.value,
                kind.value,
                session.turn_id,
                history_len=len(history),
            )
            if session.paused:  # a cancelled wait never saw "resumed"
                session.paused = False
                session.emit("notification.clear", {"key": self.PAUSE_KEY}, importance="essential")
            # a provider outage is not a correctness failure: the scope log keeps it apart from "error"
            scope_outcome = (
                "outage"
                if isinstance(session.last_exc, (AllProvidersUnreachable, ChainExhausted)) and status == "error"
                else status
            )
            if not prompt_blocked:  # a prompt a hook refused is not a task: nothing to log or learn from
                self.autonomy.finish(session, gate, scope_outcome, final_text, text)
            if self.learning.enabled and not prompt_blocked:
                self.learning.spawn(
                    self.learning.turn_finished(session, status, unattended=session.goal_continuation)
                )  # read now: the flag is cleared before the spawned task runs
            if cancelled:
                # /stop and Esc cancel the turn task, so the message.complete below never runs; the TUI holds its input
                # on "interrupting…" until it hears one (a queued message waited for a second Esc)
                session.emit(
                    "message.complete",
                    {"text": final_text, "usage": {}, "status": "interrupted", "error": None, "state": "completed"},
                )
                session.emit("status.update", {"kind": "status", "text": "", "state": "completed"})

        session.last_error = error or ""
        session.last_api_calls = sum(1 for m in loop.turn_messages if m.role == "assistant")
        session.last_tool_calls = sum(len(m.tool_calls) for m in loop.turn_messages if m.role == "assistant")
        # Foreground turns too: the agent view and the strip show a turn that ended in an error as failed, not idle.
        session.run_result = {"done": "completed", "interrupted": "completed"}.get(status, "failed")
        if status == "needs_input":
            session.run_result = None
        for msg in reversed(loop.turn_messages):
            if msg.role == "assistant" and msg.usage:
                usage = msg.usage
                break

        session.emit(
            "message.complete",
            {
                "text": final_text,
                "usage": self._usage_with_context(session, usage),
                "status": status,
                "error": error,
                "error_surface": _error_surface(session.last_exc) if status == "error" else None,
                "state": session.state,
            },
        )
        session.emit("status.update", {"kind": "status", "text": "", "state": session.state})
        if run_stop_hook:
            # The Stop hook cannot change the turn (a hook that asks to continue is logged and ignored), so the client
            # no longer waits for it: message.complete went out above. The session lock is still held, so the next
            # prompt and the goal's judge run after it, as before.
            session.stop_hook_running = True
            try:
                stopped = await loop.hooks.run("Stop", {"stop_hook_active": False})
                if stopped.blocked:  # Claude Code would continue the turn; k3code ends it and logs the reason
                    logger.info("a Stop hook asked to continue the turn (not supported): %s", stopped.reason[:200])
            except Exception:  # noqa: BLE001 - the turn is over; a failing hook must not turn it into an error
                logger.exception("the Stop hook failed")
            finally:
                session.stop_hook_running = False
        if (
            status == "done"
            and not prompt_blocked
            and not session.stored.title
            and autonomy_cfg(self.config)["auto_title"]
        ):
            task = asyncio.create_task(self._auto_title(session, text))
            self._side_tasks.add(task)
            task.add_done_callback(self._side_tasks.discard)
        return status, final_text

    #: Seconds shutdown waits for a Stop hook that is still running after its turn's answer.
    STOP_HOOK_GRACE_S = 10.0

    #: Minimum seconds between mid-turn checkpoints (each one rewrites the session's message list).
    CHECKPOINT_EVERY_S = 3.0

    async def _persist_turn(self, session: LiveSession, loop: AgentLoop) -> None:
        """Write the loop's conversation so far to the session store. Never raises: it runs in ``finally``.

        The JSON encoding and the SQLite write run on the store's writer thread, queued behind any checkpoint still
        in flight (so a stale checkpoint can never land after this write). Shielded: a cancellation of the turn
        (``/stop``, shutdown) must not drop the write."""
        try:
            session.last_checkpoint = time.monotonic()
            pending = session.checkpoint_task
            if pending is not None and not pending.done():
                await asyncio.wait({pending})  # its write is queued first, so it cannot land after this one
            if loop.turn_messages:
                stored = session.stored
                if (tools := getattr(loop, "tools", None)) is not None and (active := tools.active_names()) != (
                    stored.meta.get("mcp_active") or []
                ):
                    stored.meta["mcp_active"] = active  # the MCP tools this conversation switched on
                if (stamps := getattr(session, "file_stamps", None)) is not None and (seen := stamps.export()):
                    stored.meta["file_stamps"] = seen  # what it has read: a resumed session starts from it
                stored.messages = _serialize_messages(loop.turn_messages)
                self._save_request_state(session, stored)
                stored.model = stored.model or self._chain_key or self.config.default_model
                snapshot = dataclasses.replace(
                    stored, meta=copy.deepcopy(stored.meta), usage=copy.deepcopy(stored.usage)
                )
                await asyncio.shield(self._write_snapshot(stored, snapshot))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed checkpoint must not mask the turn's own outcome
            logger.exception("could not persist the session")
            self._warn_persist_failed(session)

    @staticmethod
    def _save_request_state(session: LiveSession, stored: Any) -> None:
        """Put into the meta what the session builds its requests from and a resumed, forked or restarted one would
        otherwise guess afresh: the estimate's correction, what is elided, the prompt snapshot. A key is dropped when
        there is nothing in it any more (a compaction or /clear emptied it)."""
        if not getattr(session, "token_ratio_stale", True):
            stored.meta["token_ratio"] = {"ratio": round(session.token_ratio, 3), "model": session.token_ratio_model}
        for key in ("elided", "elide_notes"):
            stored.meta.pop(key, None)
        stored.meta.update(
            _elision_meta(stored.messages, getattr(session, "elided", ()), getattr(session, "elide_notes", {}))
        )
        if hasattr(session, "prompt_frozen") and (snapshot := _snapshot_meta(session)) is not None:
            stored.meta["prompt_snapshot"] = snapshot
        else:
            stored.meta.pop("prompt_snapshot", None)

    async def _write_snapshot(self, stored: Any, snapshot: Any) -> None:
        """Write ``snapshot`` on the writer thread, then repair what it may have undone. A command that saved newer
        meta or a new title on the loop thread before the writer got to the snapshot (a Shift-Tab, /autocompact) was
        overwritten by it; the live values go back, without the transcript. Shielded with the write, so a cancelled
        turn does not leave the stale snapshot as the last writer."""
        await self.store.save_async(snapshot)
        if stored.meta == snapshot.meta and stored.title == snapshot.title:
            return
        try:
            self.store.save_meta(stored.session_id, stored.meta, stored.title)
        except Exception:  # noqa: BLE001 - may run detached, after the store closed (shutdown)
            logger.warning("could not refresh the meta of session %s after its write", stored.session_id, exc_info=True)

    def _warn_persist_failed(self, session: LiveSession) -> None:
        """Tell the user, once per session, that the transcript is not being saved."""
        if session.persist_warned:
            return
        session.persist_warned = True
        session.emit(
            "notification.show",
            {
                "text": "Could not save this session: new messages are not being stored (see the gateway log)",
                "level": "error",
                "kind": "persist",
                "key": f"k3.persist.{session.session_id}",
            },
            importance="essential",
        )

    def session_advisor(self, session: LiveSession) -> str:
        """The session's advisor: a model key, ``advisor.STRONG`` or "" when off (``/advisor``)."""
        return advisor.session_advisor(session.stored.meta, autonomy_cfg(self.config).get("advisor", ""))

    def _register_advisor(self, loop: AgentLoop, session: LiveSession) -> None:
        """Claude Code's advisor mode: with a session advisor set, the main model gets an ``advisor`` tool that asks
        that stronger model about the conversation so far. Registered per loop, so ``/advisor`` applies next turn."""
        key = self.session_advisor(session)
        if not key:
            return

        async def handler(args: dict[str, Any], **_: Any) -> dict[str, Any]:
            question = str(args.get("question") or "").strip()
            if not question:
                return {"error": "advisor needs a question"}
            messages = _serialize_messages(loop.turn_messages) if loop.turn_messages else session.stored.messages
            try:
                ctx = await advisor.condensed_context(
                    self.model_caller,
                    messages,
                    threshold=int(autonomy_cfg(self.config)["advisor_compact_chars"]),
                    session_id=session.session_id,
                )
                extra = str(args.get("context") or "").strip()
                if extra:
                    ctx = f"{ctx}\n\nExtra context from the agent:\n{extra}"
                text = await advisor.advise(
                    self.model_caller,
                    ctx,
                    question,
                    session_id=session.session_id,
                    system=advisor.TOOL_SYSTEM,
                    router=None if key == advisor.STRONG else self._router_for(key),
                    max_tokens=1500,
                    timeout=180,
                )
            except Exception as exc:  # noqa: BLE001 - the main model carries on without advice
                return {"error": f"advisor unavailable: {type(exc).__name__}: {exc}"}
            label = "strong tier" if key == advisor.STRONG else key
            return {"content": f"Advisor ({label}):\n{text}" if text else "The advisor returned no advice."}

        loop.tools.register(advisor.TOOL_SPEC, handler)

    def _checkpoint_turn(self, session: LiveSession) -> None:
        """After a tool result: persist the in-flight turn (throttled, one at a time, off the event loop) so kill -9,
        an OOM kill or a reboot loses seconds of work instead of the whole turn."""
        loop = session.loop
        if loop is None or not loop.turn_messages:
            return
        if time.monotonic() - session.last_checkpoint < self.CHECKPOINT_EVERY_S:
            return
        if session.checkpoint_task is not None and not session.checkpoint_task.done():
            return
        session.last_checkpoint = time.monotonic()
        messages = _serialize_messages(loop.turn_messages)
        session.stored.messages = messages
        session.stored.model = session.stored.model or self._chain_key or self.config.default_model
        session.checkpoint_task = asyncio.create_task(self._write_checkpoint(session, messages, session.stored.model))

    async def _write_checkpoint(self, session: LiveSession, messages: list[dict[str, Any]], model: str) -> None:
        try:
            await self.store.checkpoint_async(session.session_id, messages, model)
        except Exception:  # noqa: BLE001 - the next checkpoint or the end of the turn writes it again
            logger.exception("could not checkpoint the session")
            self._warn_persist_failed(session)

    def autocompact_policy(self, session: LiveSession) -> AutoCompact:
        """When this session compacts on its own: its own /autocompact --session setting, else the config's."""
        return autocompact_policy(self.config, session.stored.meta.get("autocompact"))

    async def compact_session(
        self, session: LiveSession, *, instructions: str = "", pin: dict[str, Any] | None = None
    ) -> int:
        """Fold the older part of the session's conversation into a summary (the cheap ``compaction`` tier); returns how
        many messages were folded (0 = nothing to fold). Raises when the summary call fails. What stays verbatim is
        ``context.keep_messages``; ``instructions`` is what the summary should focus on (``/compact <focus>``);
        ``pin``: the prompt of the turn in progress, kept verbatim after the summary when it is folded."""
        cfg = {**CONTEXT_DEFAULTS, **dict(getattr(self.config, "context", None) or {})}
        snapshot = list(session.stored.messages)
        new, folded = await compact_messages(
            self.model_caller,
            snapshot,
            keep=int(cfg["keep_messages"]),
            session_id=session.session_id,
            max_input_chars=int(cfg["compact_input_chars"]),
            instructions=instructions,
            pin=pin,
            max_input_tokens=int(self._summary_window(session) * SUMMARY_INPUT_SHARE),
            window=context_window(self.config, self._compaction_model(session)),
        )
        if not folded:
            return 0
        current = session.stored.messages
        if current[: len(snapshot)] != snapshot:  # /clear or another rewrite ran during the summary call: it wins
            return 0
        new = self._with_todos(session, snapshot[:folded], new)
        session.stored.messages = [*new, *current[len(snapshot) :]]  # what was appended meanwhile stays
        if isinstance(session.stored.meta.get("goal_started"), dict):
            session.stored.meta["goal_started"]["message"] = 0  # the indices moved; the summary covers the goal's start
        session.elided.clear()  # the history is new: nothing of it is elided yet
        session.elide_notes.clear()
        for key in ("elided", "elide_notes", "token_ratio"):  # the meta kept them for the history that is gone
            session.stored.meta.pop(key, None)
        session.token_ratio_stale, session.token_samples = True, 0  # a summary is not what the ratio was measured on
        session.reads.clear()  # and the results it pointed at may be folded
        session.pinned_reads.clear()
        session.refresh_prompt_inputs()  # the provider's cache of it is gone anyway: show what was written since
        session.advisor_memo.clear()
        session.stored.meta["compactions"] = int(session.stored.meta.get("compactions") or 0) + 1
        self.store.save(session.stored)
        logger.info("compacted %d messages of session %s (%d left)", folded, session.session_id, len(new))
        self._track_unfoldable(session)
        return folded

    def _track_unfoldable(self, session: LiveSession) -> None:
        """After a compaction: remember the size of the request when it is still at or above the auto-compact limit
        (what compaction cannot fold already passes it: see LiveSession.compact_unfoldable), else forget the spell."""
        policy = self.autocompact_policy(session)
        after = self._request_tokens(session)
        over = policy.enabled and after >= compact_threshold(self.config, self._compaction_model(session), policy)
        # the conversation alone: what every request carries is measured again at each turn (the first turn of a daemon
        # replaces its estimate by the built loop's), and that must not look like growth of the history
        session.compact_unfoldable = after - session.overhead_tokens if over else 0
        session.compact_unfoldable_noted = session.compact_unfoldable_noted and over

    @staticmethod
    def _unfoldable_size(session: LiveSession) -> int:
        """The request's size right after the last compaction when that left it over the limit, else 0."""
        return session.compact_unfoldable + session.overhead_tokens if session.compact_unfoldable else 0

    def _announce_unfoldable(self, session: LiveSession, threshold: int) -> None:
        """Tell the user, once per spell, that the auto-compact limit is below what compaction cannot fold."""
        if session.compact_unfoldable_noted:
            return
        session.compact_unfoldable_noted = True
        session.emit(
            "notification.show",
            {
                "text": (
                    f"auto-compact limit {threshold:,} is below what every request carries "
                    f"(about {self._unfoldable_size(session):,} tokens after compaction): raise it with /autocompact"
                ),
                "level": "info",
                "kind": "ttl",
                "ttl_ms": 15000,
                "key": f"k3.compact_unfoldable.{session.session_id}",
            },
        )

    @staticmethod
    def _with_todos(
        session: LiveSession, folded: list[dict[str, Any]], new: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """``new`` (the compacted history) with the todo list added to its summary when the call that wrote the list was
        folded: the model reads the list from that call's arguments, so folding it left the model without a plan the
        panel still showed. Not when the list was cleared, when the kept tail has a todo call of its own, or when the
        list is not from this conversation (a ``/clear`` leaves it in the meta, and nothing folded mentions it)."""
        todos = session.stored.meta.get("todos") or []

        def calls_todo(messages: list[dict[str, Any]]) -> bool:
            return any(c.get("name") == "todo" for m in messages for c in (m.get("tool_calls") or []))

        def summary_has_list(m: dict[str, Any]) -> bool:
            content = str(m.get("content") or "")
            return content.startswith(SUMMARY_PREFIX) and "\nTodos (" in content

        summary_at = next(
            (i for i, m in enumerate(new) if str(m.get("content") or "").startswith(SUMMARY_PREFIX)), None
        )
        if not todos or summary_at is None:
            return new
        if not (calls_todo(folded) or any(summary_has_list(m) for m in folded)) or calls_todo(new[summary_at + 1 :]):
            return new
        summary = new[summary_at]
        # the list the summary model restated from the earlier summary is replaced, not doubled
        content = TODO_BLOCK_RE.sub("", str(summary.get("content") or "")).rstrip()
        return [
            *new[:summary_at],
            {**summary, "content": f"{content}\n\n{render_todos(todos)}"},
            *new[summary_at + 1 :],
        ]

    async def _maybe_compact(
        self, session: LiveSession, *, force: bool = False, pin: dict[str, Any] | None = None
    ) -> int:
        """Fold the older part of the conversation into a summary once it is large; returns how many messages folded.

        Nothing compacted automatically, so a `/loop` living in one session grew its history forever: every tick
        re-sent and re-stored all of it, and on a real model the context window was exceeded after a few hundred ticks
        and every later tick failed with ContextOverflow. Runs on the cheap ``compaction`` tier; failures are logged and
        the turn goes on. ``/autocompact`` decides whether and at what size (the policy); ``force`` (the retry after a
        ContextOverflow) skips the size test. After a fold that leaves the request over the limit (see
        LiveSession.compact_unfoldable) the next automatic one waits for the history to grow; ``force`` never waits.
        ``pin``: see compact_session.
        """
        if self.halted:  # the compaction call is a model call too
            return 0
        policy = self.autocompact_policy(session)
        if not force:
            if not policy.enabled or time.monotonic() < session.compact_retry_at:
                return 0
            model = self._compaction_model(session)
            threshold = compact_threshold(self.config, model, policy)
            used = self._request_tokens(session)
            if used < threshold:
                session.compact_unfoldable = 0  # a spell of the limit being below what compaction leaves is over
                session.compact_unfoldable_noted = False
                return 0
            if session.compact_unfoldable and used < compact_resume_at(
                self._unfoldable_size(session), threshold, context_window(self.config, model)
            ):
                # the last compaction left the request over the limit already (what it cannot fold passes it): another
                # summary now would fold next to nothing, and would again on every turn. Wait for the history to grow.
                self._announce_unfoldable(session, threshold)
                return 0
        session.emit("status.update", {"kind": "compacting", "text": "compacting the conversation", "state": "working"})
        try:
            folded = await self.compact_session(session, pin=pin)
        except Exception as exc:  # noqa: BLE001 - e.g. every provider rate-limited: the turn proceeds with the long history
            logger.warning("automatic compaction failed", exc_info=True)
            session.compact_retry_at = time.monotonic() + COMPACT_RETRY_AFTER_S
            folded = 0
            session.emit(
                "notification.show",
                {
                    "text": f"Compaction failed ({type(exc).__name__}: {str(exc)[:200]}): the conversation is sent "
                    f"whole; the next try is in {COMPACT_RETRY_AFTER_S:.0f} s",
                    "level": "warning",
                    "kind": "ttl",
                    "ttl_ms": 15000,
                    "key": f"k3.compact_failed.{session.session_id}",
                },
            )
        finally:  # always: the TUI shows "compacting" until it hears this
            session.emit("status.update", {"kind": "compacted", "text": "compaction done", "state": session.state})
        if folded:
            self.emit_context(session)
            session.emit(
                "notification.show",
                {
                    "text": f"Context compacted: {folded} older messages summarized",
                    "level": "info",
                    "kind": "ttl",  # an event, not a state: left standing it stayed in the notification area for good
                    "ttl_ms": 15000,
                    "key": "k3.compact",
                },
            )
            if session.compact_unfoldable:  # still over the limit after the fold: say so once, and see the gate above
                limit = compact_threshold(self.config, self._compaction_model(session), policy)
                self._announce_unfoldable(session, limit)
        return folded

    def context_fields(self, session: LiveSession, *, used: int | None = None) -> dict[str, Any]:
        """Where the session stands in its context window, for the status bar and /usage: tokens ``used`` (the last
        call's prompt and answer when the provider reported them, else the estimate of the next request), the model's
        window, the share used, where automatic compaction starts (None = off) and how often it has run."""
        model = self._compaction_model(session)
        window = context_window(self.config, model)
        estimated = used is None
        used = self._request_tokens(session) if used is None else used
        policy = self.autocompact_policy(session)
        sent = session.prompt_tokens_total
        return {
            **({"cache_hit_pct": round(100 * session.cache_read_tokens_total / sent)} if sent else {}),
            "context_used": used,
            "context_max": window,
            "context_percent": min(100, round(100 * used / window)) if window else 0,
            "context_estimated": estimated,
            "autocompact_at": compact_threshold(self.config, model, policy) if policy.enabled else None,
            "compressions": int(session.stored.meta.get("compactions") or 0),
        }

    def _usage_with_context(self, session: LiveSession, usage: Usage) -> dict[str, Any]:
        """A call's usage payload plus the context fields (the call's own count when it has one)."""
        reported = usage.prompt_tokens + usage.completion_tokens if usage.prompt_tokens else None
        return {**_usage_payload(usage), **self.context_fields(session, used=reported)}

    def emit_context(self, session: LiveSession) -> None:
        """Tell the clients the session's context size (after a compaction, /clear or a model switch)."""
        session.emit("session.usage", {"usage": self.context_fields(session)})

    def _request_tokens(self, session: LiveSession, messages: list[dict[str, Any]] | None = None) -> int:
        """Estimated size of the session's next request: the stored conversation (its stored system entry is not sent:
        the loop builds a fresh one; ``messages`` stands in for it) plus the system prompt and the tool schemas every
        request carries."""
        if not session.overhead_tokens:  # no loop built yet in this daemon: the prompt and the built-in tools
            prompt = self._system_prompt_for(session)
            session.overhead_tokens = overhead_tokens(prompt, build_tool_registry().specs())
        stored = session.stored.messages if messages is None else messages
        conversation = [m for m in stored if m.get("role") != "system"]
        counted = self._conversation_tokens(session, conversation)
        # the correction is for the conversation: the system prompt and the tools are estimated well, and the ratio was
        # measured without them
        return int(counted * self._token_ratio(session)) + session.overhead_tokens

    def _conversation_tokens(self, session: LiveSession, conversation: list[dict[str, Any]]) -> int:
        """Estimated tokens of ``conversation`` as the session's loop sends it: results elided or cut down."""
        window = context_window(self.config, self._compaction_model(session))
        return _estimate_tokens(conversation, tool_output_limit(self.config), session.elided, window)

    @staticmethod
    def _token_ratio(session: LiveSession) -> float:
        """The session's correction for the token estimate; a switch to another model key starts it again."""
        if session.token_ratio_model != (session.stored.model or ""):
            session.token_ratio_model = session.stored.model or ""
            session.token_ratio, session.token_ratio_stale, session.token_samples = 1.0, True, 0
        return session.token_ratio

    def _calibrate(self, session: LiveSession, usage: Usage) -> None:
        """Compare the prompt the provider counted with the estimate of the request it counted (the loop's messages: the
        reply that ends the call is not in them yet), so that the compaction trigger and the status bar judge by what
        the provider will say. Not for claude-cli (the only provider that reports a cost): its count includes the
        Claude Code prompt and tools, which this estimate does not know."""
        loop = session.loop
        # a call made before the turn's own loop has run (the planning turn reports through here) is not a request of
        # its conversation
        if loop is None or not loop.turn_messages or usage.prompt_tokens < CALIBRATE_MIN_TOKENS:
            return
        if usage.cost_usd is not None:
            return
        self._token_ratio(session)
        session.token_samples += 1
        if session.token_samples > 1 and session.token_samples % CALIBRATE_EVERY != 1:
            return
        conversation = [m for m in _serialize_messages(loop.turn_messages) if m["role"] != "system"]
        counted = self._conversation_tokens(session, conversation)
        carried = usage.prompt_tokens - session.overhead_tokens
        if counted < CALIBRATE_MIN_TOKENS or carried <= 0:
            return
        sample = min(RATIO_MAX, max(RATIO_MIN, carried / counted))
        if not session.token_ratio_stale:
            sample = session.token_ratio * (1 - RATIO_WEIGHT) + sample * RATIO_WEIGHT
        session.token_ratio = sample
        session.token_ratio_stale = False

    async def _auto_title(self, session: LiveSession, first_message: str) -> None:
        """Name a fresh session on the ``title`` task kind; best-effort, never surfaces errors."""
        title = await make_title(self.model_caller, first_message, session_id=session.session_id)
        if title and not session.stored.title:
            session.stored.title = title
            self.store.save_meta_of(session.stored)
            session.emit("session.title", {"session_id": session.session_id, "title": title})

    def _announce_tool(self, session: LiveSession, tc: ToolCall) -> None:
        """tool.generating + tool.start and the usage row, once per call id."""
        if tc.id in session.announced_tools:
            return
        session.announced_tools.add(tc.id)
        session.emit("tool.generating", {"name": tc.name})
        args = tc.arguments or {}
        # `context` is the line a tool row shows next to the tool's name (the file, the command, the pattern) and
        # `args_text` the Args block of the expanded row: the TUI read both and the core sent neither
        context = str(args.get("command") or args.get("path") or args.get("pattern") or args.get("url") or "")[:120]
        session.emit(
            "tool.start",
            {
                "tool_id": tc.id,
                "name": tc.name,
                "args": tc.arguments,
                "context": context.splitlines()[0] if context else "",
                "args_text": json.dumps(tc.arguments, ensure_ascii=False, indent=2)[:2000],
            },
        )

    def _on_stream_event(self, session: LiveSession, event: StreamEvent) -> None:
        if event.type == "tool_call" and event.tool_call:
            self._announce_tool(session, event.tool_call)
        elif event.type == "done" and event.message:
            msg = event.message
            if msg.role == "tool":
                now = time.monotonic()
                seconds = now - session.tool_mark if session.tool_mark is not None else 0.0
                self.usage.record("tool", session=session.session_id, detail=msg.name or "", seconds=seconds)
                session.tool_mark = now
                self._checkpoint_turn(session)
                payload = {
                    "tool_id": msg.tool_call_id or "",
                    "name": msg.name or "",
                    "result_text": msg.content or "",
                    "result": {"content": msg.content},
                    "duration_s": round(seconds, 2),
                }
                if msg.name == "todo":  # the TUI's todo panel reads the list from tool.complete
                    payload["todos"] = list(session.stored.meta.get("todos") or [])
                session.emit("tool.complete", payload)
            elif msg.role == "assistant":
                provider, model = session.last_entry
                u = msg.usage
                self.usage.record(
                    "call",
                    session=session.session_id,
                    provider=provider,
                    model=model,
                    tokens_in=u.prompt_tokens if u else 0,
                    tokens_out=u.completion_tokens if u else 0,
                    cost_usd=u.cost_usd if u else None,
                    cache_read=u.cache_read_tokens if u else 0,
                    cache_write=u.cache_creation_tokens if u else 0,
                    tier=session.last_tier,
                    task_kind=session.current_kind,
                    turn=session.turn_id,
                    seconds=time.monotonic() - session.call_started if session.call_started is not None else 0.0,
                )
                if u:
                    session.prompt_tokens_total += u.prompt_tokens
                    session.cache_read_tokens_total += u.cache_read_tokens
                    self._calibrate(session, u)
                session.call_started = None
                session.tool_mark = time.monotonic()  # the loop executes the tool calls right after this message
                if u:
                    session.emit("session.usage", {"usage": self._usage_with_context(session, u)})
                # openai_compat and anthropic stream no tool_call events: their calls arrive on this message only
                for tc in msg.tool_calls:
                    self._announce_tool(session, tc)
                session.announced_tools.clear()

    async def _approval_callback_for(self, session: LiveSession) -> Any:
        async def approve(
            tool_name: str, arguments: dict[str, Any], decision: Any = None, *, cwd: Path | str | None = None
        ) -> ApprovalResult:
            # ``cwd``: where the call runs. This callback is shared by the session's sub-agents, and one in a worktree
            # resolves its relative paths there, so the preview read the parent's file (or none) for its diff.
            decision = decision or session.perms.decide(tool_name, arguments)
            # a file the person must confirm every time (.env, .husky/ ...) is never allowed by a rule: offering
            # "session" or "always" would write one that can never apply
            once_only = bool(decision.always_ask)
            rules = [] if once_only else suggest_rules(tool_name, decision)
            if _in_throwaway_worktree(rules):
                # a sub-agent's file in its fan-out worktree (.k3code/worktrees/<id>/...): the rule would name a
                # directory that is removed when the child ends, so "session" and "always" (which writes it into the
                # project's config.yaml) would leave a rule that can never apply again
                once_only, rules = True, []
            pattern = ", ".join(r.pattern for r in rules)
            self.usage.record("approval", session=session.session_id, detail=tool_name)
            try:
                # one question at a time per session: sub-agents running side by side each ask for their own tools
                async with session.approval_lock:
                    # made once it is this call's turn to ask: an edit approved meanwhile has changed the file
                    preview = await _change_preview(tool_name, arguments, cwd or session.perms.cwd)
                    result = await self._ask_client(
                        "approval",
                        {
                            "request_id": next_request_id("approval"),
                            "command": _command_for_tool(tool_name, arguments),
                            # what an edit/write would change (a unified diff); clients that predate it ignore it
                            **({"preview": preview} if preview else {}),
                            "description": decision.message
                            or arguments.get("description")
                            or arguments.get("path")
                            or arguments.get("pattern")
                            or "",
                            "choices": ["once", "deny"] if once_only else ["once", "session", "always", "deny"],
                            "allow_permanent": not once_only,
                            "allow_session": not once_only,
                            "tool_name": tool_name,
                            "pattern": pattern,
                        },
                        session.session_id,
                    )
            except (asyncio.CancelledError, RuntimeError):
                return ApprovalResult("deny", "approval request cancelled")
            choice = str(result.get("choice", "deny")).lower()
            if choice not in ("once", "session", "always"):
                choice = "deny"
            elif once_only:
                choice = "once"  # a client that answers "session"/"always" anyway: nothing is remembered
            reason = str(result.get("reason") or result.get("text") or "").strip()
            if choice in ("session", "always"):
                session.perms.session_rules.extend(rules)
            if choice == "always" and rules:
                persist_rules(project_config_path(session.perms.cwd), rules)
            self.learning.approval(
                session, tool_name, pattern, choice, reason=reason, command=_command_for_tool(tool_name, arguments)
            )
            return ApprovalResult(choice, reason)

        return approve

    def _plan_callback_for(self, session: LiveSession) -> Any:
        async def approve_plan(plan: str) -> str | None:
            self.usage.record("approval", session=session.session_id, detail="exit_plan")
            try:
                result = await self._ask_client(
                    "approval",
                    {
                        "request_id": next_request_id("approval"),
                        "command": "Approve this plan?",
                        "description": plan,
                        "choices": ["once", "session", "deny"],
                        "labels": {
                            "once": "Approve plan (ask before edits)",
                            "session": "Approve plan + accept edits",
                            "deny": "Keep planning",
                        },
                        "allow_permanent": False,
                        "allow_session": True,
                        "tool_name": "exit_plan",
                    },
                    session.session_id,
                )
            except (asyncio.CancelledError, RuntimeError):
                return None
            choice = str(result.get("choice", "deny")).lower()
            target = {"once": "default", "session": "accept-edits"}.get(choice)
            self.learning.record(
                "plan",
                session,
                subject="exit_plan",
                choice=choice,
                detail={
                    "has_verification": _has_verification(plan),
                    "edited": bool(result.get("edited")),
                    "risk": "",
                    "plan_len": len(plan),
                },
            )
            if target:
                session.set_mode(PermissionMode(target))
            return target

        return approve_plan

    # ── services for command handlers ─────────────────────────────────

    async def clarify(self, question: str, choices: list[str], session_id: str | None) -> dict[str, Any]:
        """Ask the client a multiple-choice question (``clarify`` server request)."""
        return await self._ask_client("clarify", {"question": question, "choices": choices}, session_id or "")

    def _turn_of(self, session_id: str) -> str:
        """The turn id in flight for a live session ("" when it is idle or unknown)."""
        live = self.live.get(session_id)
        return live.turn_id if live is not None else ""

    def apply_file_config(self, cwd: str | Path | None = None) -> None:
        """Re-read config files into the live settings.

        Keys present in a file are replaced by the file's value. A key that was in a file at the previous read and is
        gone now (removed by hand or rolled back by an experiment) is reset to its fresh value, not left as it was.

        This config is shared by every session, so only the user config and the server's own project apply to it; the
        project config of the session ``cwd`` (a command's session) stays out (it leaked `permission_mode: yolo` and
        `max_turns` into every new session). A session gets its own project rules from its PermissionState.
        ``cwd`` only picks the project whose ``.mcp.json`` the MCP servers come from.
        """
        base = default_project_dir()
        fresh = load_config(project_dir=base)
        keys = _file_keys(base)
        changed = (keys | self._file_keys) & set(Settings.model_fields)
        if "providers" in changed and fresh.providers == self.config.providers:
            # Same chain: keep the list, so the providers and routers built from it stay as they are (a turn of
            # another session may be calling them) instead of being rebuilt by the next turn.
            changed.discard("providers")
        for key in changed:
            setattr(self.config, key, getattr(fresh, key))
        self._file_keys = keys
        if "providers" in changed:
            self.router = None
            self._tiers = None
        self.mcp.configure(mcpjson.merged(self.config.mcp.servers, Path(cwd) if cwd else base))

    def activate_session(self, session_id: str) -> LiveSession | None:
        stored = self.store.get(session_id)
        if stored is None:
            return None
        live = self.live_for(stored)
        self.session = live  # attaches the requesting client (setter)
        live.emit("session.info", live.live_info())
        return live

    def _router_for(self, key: str | None) -> Router:
        """A throwaway-cache router for one-shot calls (review, goal judge), independent of the turn router."""
        key = key or self.config.default_model
        cache: dict[str, Router] = self.__dict__.setdefault("_oneshot_routers", {})
        if key not in cache:
            if not self.providers:
                self.providers = make_providers(self.config.providers)
            chain = build_chain(self.providers, _resolve_model_specs(self.config, key))
            cache[key] = Router(
                chain, cooldowns=self.cooldowns, on_event=self._on_router_event, **router_options(self.config)
            )
        return cache[key]

    async def oneshot(
        self,
        system: str,
        user: str,
        *,
        model_key: str | None = None,
        max_tokens: int = 2048,
        kind: TaskKind | None = None,
        session_id: str = "",
    ) -> str:
        """Sub-turn: one tool-less completion; returns the text.

        With a task ``kind`` the call goes through :class:`ModelCaller` (tier/kind usage rows, the session and day
        budgets); an explicit ``model_key`` names the chain it is sent to (no tier policy or escalation then, but still
        booked and charged: a configured ``goal.judge_model`` used to leave no trace). Without a ``kind``: a plain chain
        for ``model_key``.
        """
        messages = [Message(role="system", content=system), Message(role="user", content=user)]
        if kind is not None:
            router = self._router_for(model_key) if model_key is not None else None
            res = await self.model_caller.complete(
                kind, messages, session_id=session_id, max_tokens=max_tokens, router=router
            )
            return res.text
        router = self._router_for(model_key)
        # the chain already holds each provider's resolved model; an explicit model= would override it with the key
        msg = await router.complete(messages, [], max_tokens=max_tokens)
        return msg.content or ""

    def _goal_completer(self, session: LiveSession) -> Any:
        override = self.config.goal.judge_model
        explicit = override if override and override != "cheap" else None  # "cheap" = the policy's default

        async def complete(system: str, user: str) -> str:
            return await self.oneshot(
                system,
                user,
                model_key=explicit,
                kind=TaskKind.GOAL_JUDGE,
                session_id=session.session_id,
                max_tokens=512,
            )

        return complete

    def _goal_reviewer(self, session: LiveSession) -> Any:
        """M4a: a strong-tier advisor veto on ``done`` (``autonomy.advisor_on_goal``) in the permission modes of
        ``autonomy.advisor_modes`` (auto by default, as for the plan critique); None when it does not apply. It used
        to ignore the modes: every goal in every mode paid a strong-tier call at each "done"."""
        cfg = autonomy_cfg(self.config)
        if not self.autonomy.advisor_applies(session, "advisor_on_goal"):
            return None

        async def review(goal: str) -> tuple[bool, list[str]]:
            # only what happened since the goal was set: the whole session (hours of unrelated work, up to 24k
            # characters, or a cheap-tier summary of more) was sent to the strong tier on every "done"
            start = int((session.stored.meta.get("goal_started") or {}).get("message") or 0)
            messages = session.stored.messages
            ctx = await advisor.condensed_context(
                self.model_caller,
                messages[start:] if 0 < start < len(messages) else messages,
                threshold=int(cfg["advisor_compact_chars"]),
                session_id=session.session_id,
                memo=session.advisor_memo,  # a goal vetoed again and again summarizes only what is new each time
            )
            return await advisor.review_done(self.model_caller, goal, ctx, session_id=session.session_id)

        return review

    def goal_manager(self, session: LiveSession) -> GoalManager:
        def load() -> dict[str, Any] | None:
            return session.stored.meta.get("goal")

        def save(state: dict[str, Any] | None) -> None:
            meta = session.stored.meta
            if state is None:
                meta.pop("goal", None)
                meta.pop("goal_started", None)
            else:
                meta["goal"] = state
                started = meta.get("goal_started") or {}
                if started.get("at") != state.get("created_at"):  # a new goal: where in the history it begins
                    meta["goal_started"] = {"at": state.get("created_at"), "message": len(session.stored.messages)}
            self.store.save_meta_of(session.stored)  # not save(): that rewrote the whole transcript on the loop

        mgr = GoalManager(
            load,
            save,
            default_max_turns=self.config.goal.max_turns,
            default_gate_max_retries=self.config.goal.gate_max_retries,
            persist=self.config.goal.persist,
        )
        mgr.on_wait = lambda text: session.emit(
            "notification.show", {"text": text, "level": "info", "kind": "info", "key": "goal"}
        )
        return mgr

    async def kick_goal(self, live: LiveSession, *, source: str) -> bool:
        """Start the next turn of an active goal that has no live turn. Boot resume and the watchdog both use this.

        Refused while halted or in restart-storm safe mode. Every kick is counted: once MAX_KICKS_PER_WINDOW kicks
        happened within KICK_WINDOW_S, the goal is parked (paused) instead of kicked again.
        """
        if self.halted or self.background_paused:
            return False
        mgr = self.goal_manager(live)
        goal = mgr.state
        if goal is None or goal.status != "active":
            return False
        if live.turn_in_flight:
            return False  # a live turn is working on it already
        if goal.implicit:  # its turn loop is gone (the daemon died mid-loop): the prompt is not re-run unasked
            mgr.clear()
            self.emit_goal(live)
            return False
        now = time.time()
        if len(mgr.recent_kicks(now)) >= MAX_KICKS_PER_WINDOW:
            mgr.pause(f"parked: {MAX_KICKS_PER_WINDOW} automatic kicks in 1 h")
            self.emit_goal(live)
            self.notify_blocker(
                live.session_id,
                f"Goal parked after {MAX_KICKS_PER_WINDOW} automatic restarts within an hour ({source}). "
                "Check it, then /goal resume.",
                level="warning",
                kind="goal",
                key=f"k3.goal.parked.{live.session_id}",
            )
            return False
        mgr.record_kick(now)
        logger.info("goal in session %s kicked (%s)", live.session_id, source)
        live.turn_task = asyncio.get_running_loop().create_task(
            self._run_turn(live, mgr.kick_prompt() or goal.goal), name=f"kick-{live.session_id}"
        )
        self.emit_goal(live)
        return True

    def _sweep_keep(self) -> Callable[[str], bool] | None:
        """The sweep's "leave this row alone" test, or None without the automation engine (nothing is swept then:
        which rows a loop or automation points at cannot be told)."""
        automation = self.automation
        if automation is None:
            return None

        def keep(sid: str) -> bool:
            if sid in self.live or (self.session is not None and self.session.session_id == sid):
                return True
            if any(c.session_id == sid for c in self.clients):
                return True
            return bool(automation.references_session(sid))

        return keep

    def sweep_empty_sessions(self, *, now: float | None = None, max_age: float = EMPTY_SESSION_MAX_AGE_S) -> int:
        """Drop old empty stored sessions (every `k3code agents`, `n` and abandoned TUI start leaves one).

        The store decides what counts as empty and unclaimed; this adds what only the server knows: live sessions,
        the ones a client looks at, and those any loop or automation, paused ones included, points at.

        A standalone stdio TUI/CLI sharing sessions.db can hold an empty session open longer than ``max_age``
        (30 days) and so lose its row here. Each deleted row leaves a tombstone (``swept_sessions``, written in the
        same transaction), and ``save()`` re-inserts a missing row only when its tombstone is there, so that
        process's next save restores the session. ``save()`` is no upsert: a row removed by ``session.delete`` has
        no tombstone and stays gone. The sweep forgets tombstones after 90 days."""
        keep = self._sweep_keep()
        if keep is None:
            return 0
        swept: list[str] = []
        n = self.store.sweep_empty(now=now, max_age=max_age, keep=keep, swept=swept)
        for sid in swept:
            self.remove_session_files(sid)
        return n

    async def sweep_empty_sessions_async(
        self, *, now: float | None = None, max_age: float = EMPTY_SESSION_MAX_AGE_S
    ) -> int:
        """:meth:`sweep_empty_sessions` in small batches that yield to the event loop (same tombstones)."""
        keep = self._sweep_keep()
        if keep is None:
            return 0
        swept: list[str] = []
        n = await self.store.sweep_empty_async(now=now, max_age=max_age, keep=keep, swept=swept)
        for sid in swept:
            self.remove_session_files(sid)
        return n

    def remove_session_files(self, sid: str) -> None:
        """A deleted or swept session's files: its tool journal, transcript checkpoint and stored pastes.

        ``sid`` comes from a client (session.delete): only a plain name is used, never ``.``, ``..`` or a path (``..``
        would have made the pastes directory ``$K3CODE_HOME`` itself)."""
        if not sid or sid in (".", "..") or "/" in sid or "\\" in sid or "\0" in sid:
            return
        home = self._home()
        delete_session_journal(home, sid)
        pastes = home / PASTES_DIR / sid
        if pastes.is_dir() and not pastes.is_symlink():
            shutil.rmtree(pastes, ignore_errors=True)

    def prune_retention(self, *, now: float | None = None) -> dict[str, int]:
        """Daemon start: usage rows past ``retention.usage_days``, journal files of sessions that are not live and not
        written for ``retention.journal_days`` (the oldest ones beyond ``retention.journal_mb`` too), learning decisions
        past ``retention.decisions_days`` (debug bundles are pruned as each is written)."""
        keep = retention(self.config)
        rows = self.usage.prune(keep["usage_days"], now=now)
        files = prune_journals(
            self._home(),
            keep=lambda sid: sid in self.live,
            max_age_s=keep["journal_days"] * 86400,
            max_bytes=keep["journal_mb"] * 1024 * 1024,
            now=now,
        )
        decisions = self.learning.log.prune(keep["decisions_days"])
        return {"usage_rows": rows, "journal_files": files, "decisions": decisions}

    def compact_storage(self) -> int:
        """Daemon start: return the pages that deleted transcripts left in ``sessions.db`` to the filesystem."""
        return self.store.compact()

    async def resume_goals(self) -> int:
        """Boot: continue each goal that was active, or paused by a graceful stop, when the daemon last ran.

        Returns how many turns were started. Nothing starts while halted or in restart-storm safe mode.
        """
        if self.halted or self.background_paused:
            return 0
        for stored in self.store.with_goal_status("paused"):
            if (stored.meta.get("goal") or {}).get("paused_reason") == "daemon restart":
                self.goal_manager(self.live_for(stored)).resume(reset_budget=False)
        return len(await self.watchdog_tick(source="boot"))

    async def watchdog_tick(self, *, source: str = "watchdog") -> list[str]:
        """Re-kick every goal persisted active that has no live turn (through kick_goal, so the kick counter and the
        halt and storm guards apply). Returns the session ids kicked."""
        if self.halted or self.background_paused:
            return []
        kicked: list[str] = []
        for stored in self.store.with_goal_status("active"):  # only active goals get a live session (and a kick)
            if await self.kick_goal(self.live_for(stored), source=source):
                kicked.append(stored.session_id)
        return kicked

    def notify_session(self, live: LiveSession, text: str, *, level: str = "info", key: str = "") -> None:
        """A transient notification to the clients attached to ``live`` (essential, so focus mode keeps it)."""
        payload = {
            "text": text,
            "level": level,
            "kind": "goal",
            "key": key or f"k3.goal.{live.session_id}",
            "session_id": live.session_id,
        }
        live.emit("notification.show", payload, importance="essential")

    def notify_blocker(
        self, session_id: str, text: str, *, level: str = "warning", kind: str = "goal", key: str = ""
    ) -> int:
        """A blocker: stored until a client takes it (so it survives a restart), and shown to attached clients now.

        The text is scrubbed first: it can carry an approval command with credentials in it (S8).
        """
        text = scrub_text(text)
        blocker_id = self.blockers.add(session_id=session_id, kind=kind, text=text, level=level)
        payload = {
            "text": text,
            "level": level,
            "kind": kind,
            "key": key or f"k3.blocker.{blocker_id}",
            "session_id": session_id,
            "blocker_id": blocker_id,
        }
        live = self.live.get(session_id)
        if live is not None:
            live.emit("notification.show", payload, importance="essential")
        if any(c.session_id == session_id and not c.closed for c in self.clients):
            self.blockers.mark_delivered(blocker_id)
        return blocker_id

    def emit_goal(self, session: LiveSession) -> None:
        """Push the goal snapshot to the TUI goal bar (``session.control.update``)."""
        session.control["goal"] = self.goal_manager(session).snapshot() or ""
        session.control["revision"] += 1
        session.control["updated_at"] = time.time()
        session.emit("session.control.update", {"control": dict(session.control)})

    async def _confirm_plan(self, session: LiveSession, plan: str, risk: str) -> bool:
        """Auto mode + high-risk plan: ask once; approving does not leave auto mode."""
        self.usage.record("approval", session=session.session_id, detail="plan(high risk)")
        try:
            result = await self._ask_client(
                "approval",
                {
                    "request_id": next_request_id("approval"),
                    "command": f"Approve this {risk}-risk plan?",
                    "description": plan,
                    "choices": ["once", "deny"],
                    "labels": {"once": "Approve plan and run it", "deny": "Reject plan"},
                    "allow_permanent": False,
                    "allow_session": False,
                    "tool_name": "exit_plan",
                },
                session.session_id,
            )
        except (asyncio.CancelledError, RuntimeError):
            return False
        approved = str(result.get("choice", "deny")).lower() == "once"
        self.learning.record(
            "plan",
            session,
            subject="confirm",
            choice="approved" if approved else "rejected",
            detail={"has_verification": _has_verification(plan), "risk": risk, "edited": bool(result.get("edited"))},
        )
        return approved

    # ── commands ──────────────────────────────────────────────────────

    async def dispatch_command(self, name: str, arg: str, session_id: str | None) -> dict[str, Any]:
        """Run a slash command; returns a CommandDispatchResult-shaped dict."""
        sid = session_id or (self.session.session_id if self.session else None)
        result = await self.commands.dispatch(self, name, arg, sid)
        if result.get("type") == "exit":
            self._running = False
        return result

    # ── long-running command jobs (/ultraplan, /ultracode, /ultraresearch) ──

    def start_job(self, session: LiveSession, label: str, make_coro: Callable[[], Any]) -> None:
        """Run ``make_coro()`` as the session's turn: it shows as working, /stop interrupts it, and its returned
        text becomes the assistant message."""
        if self.halted:
            raise _InvalidParams("daemon is halted (/daemon pause); resume with /daemon resume")
        if session.turn_in_flight:
            raise _InvalidParams("a turn is already running in this session; /stop it or wait")

        async def runner() -> None:
            status, _ = await self._run_job(session, label, make_coro)
            # prompts typed while the job ran were queued: run them now, like _run_turn does after a turn
            if status != "interrupted" and (pending := self._pending_prompts(session)):
                await self._run_turn(session, pending.pop(0))

        session.turn_task = asyncio.get_running_loop().create_task(runner())

    async def _run_job(
        self, session: LiveSession, label: str, make_coro: Callable[[], Any], *, user_text: str | None = None
    ) -> tuple[str, str]:
        """The body of a job, in whatever task owns the session's turn: ``start_job``'s task, or the turn that a wake
        word or the ultracode mode turned into a job (it holds the turn lock; this never takes it).

        ``user_text``: what the user typed when that differs from ``label`` (it is what the transcript keeps).
        Returns ``(status, text)``; /stop (a cancel of the owning task) ends the job as ``interrupted``."""
        _ctx_session.set(session)
        session.needs_input = False
        session.run_result = None  # an earlier turn's outcome must not survive into this job's events
        session.streaming = True
        session.turn_started_wall = time.time()
        session.emit("message.start", {})
        session.emit("status.update", {"kind": "status", "text": label, "state": "working"})
        status, text = "done", ""
        try:
            text = await make_coro()
        except asyncio.CancelledError:
            status, text = "interrupted", f"{label} interrupted."
        except Exception as e:  # noqa: BLE001 - a failed job is reported, never crashes the gateway
            logger.exception("%s failed", label)
            status, text = "error", f"{label} failed: {e}"
            session.emit("error", {"message": text})
        finally:
            session.streaming = False
            session.call_started = None
            self.subagents.interrupt_session(session.session_id)  # nothing may outlive the job
        self._finish_job(session, label if user_text is None else user_text, status, text)
        return status, text

    def _finish_job(self, session: LiveSession, user_text: str, status: str, text: str) -> None:
        """The closing events of a job (or of a reply that is not a job) and the exchange saved to the transcript."""
        # same mapping as _run_one_turn, set before the closing events carry session.state
        session.run_result = {"done": "completed", "interrupted": "completed"}.get(status, "failed")
        session.emit("message.delta", {"text": text})
        session.stored.messages = [
            *session.stored.messages,
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": text},
        ]
        self.store.save(session.stored)
        session.emit(
            "message.complete", {"text": text, "usage": {}, "status": status, "error": None, "state": session.state}
        )
        session.emit("status.update", {"kind": "status", "text": "", "state": session.state})

    def _reply_turn(self, session: LiveSession, user_text: str, text: str) -> tuple[str, str]:
        """A turn that is only an answer, with no model call and no job (a bare wake word gets the usage line)."""
        session.emit("message.start", {})
        self._finish_job(session, user_text, "done", text)
        return "done", text

    # ── background sessions (/bg, Ctrl+B) ─────────────────────────────

    def _fresh_session_like(self, src: LiveSession, *, background: bool = False, cwd: str | None = None) -> LiveSession:
        """A new session with ``src``'s cwd (``cwd`` instead when given), model, permission mode and add-dirs."""
        stored = self.store.create(
            model=src.stored.model or self.config.default_model,
            provider=src.stored.provider or "",
            cwd=_norm_cwd(cwd) or src.stored.cwd or str(Path.cwd()),
        )
        stored.meta["mode"] = src.perms.mode.value
        stored.meta["add_dirs"] = list(src.perms.add_dirs)
        # effort and the ultracode mode are inherited and kept in the meta, so a resume of the new session has them
        if src.reasoning_effort:
            stored.meta["reasoning_effort"] = src.reasoning_effort
        if src.ultra_mode != "off":
            stored.meta["ultra_mode"] = src.ultra_mode
        if own := clean_override(src.stored.meta.get("agent_limits")):  # /maxagents is inherited too
            stored.meta["agent_limits"] = own
        if background:
            stored.meta["background"] = True
            stored.meta["origin_session"] = src.session_id
        self.store.save(stored)
        live = LiveSession(stored.session_id, stored, self)
        live.reasoning_effort = src.reasoning_effort
        self.live[live.session_id] = live
        return live

    def start_background(self, origin: LiveSession, prompt: str, cwd: str | None = None) -> LiveSession:
        """``/bg <prompt>``: run ``prompt`` in a new background session; notify ``origin`` when it ends.

        ``cwd``: the client's workspace (a TUI attached to a daemon); without it the session inherits ``origin``'s."""
        if self.halted:
            raise _InvalidParams("daemon is halted (/daemon pause); resume with /daemon resume")
        if self.background_paused:
            raise _InvalidParams("background work is paused (restart-storm safe mode); resume with /daemon resume")
        live = self._fresh_session_like(origin, background=True, cwd=cwd)
        live.stored.title = live.stored.title or " ".join(prompt.split())[:60]
        self.store.save(live.stored)
        # typed by the user: a wake word in it runs that mode here (the ultracode mode skips background sessions)
        live.turn_task = asyncio.get_running_loop().create_task(self._run_turn(live, TypedPrompt(prompt)))
        self._watch_background(live, origin.session_id)
        return live

    def background_current(self, session: LiveSession, client: Client | None) -> LiveSession:
        """Ctrl+B: the running turn keeps going as a background session; the client gets a fresh foreground one."""
        session.background = True
        session.stored.meta["background"] = True
        session.stored.meta["origin_session"] = session.session_id
        if session.loop is not None:
            session.loop.background = True  # bash is sandboxed from now on
        self.store.save_meta_of(session.stored)
        fresh = self._fresh_session_like(session)
        if client is not None:
            self.attach(client, fresh)
        session.emit("session.info", session.live_info())
        if session.turn_task is not None:
            self._watch_background(session, fresh.session_id)
        return fresh

    def _watch_background(self, live: LiveSession, notify_sid: str) -> None:
        """Tell ``notify_sid``'s clients when the background session finishes or needs input."""
        task = live.turn_task
        if task is None:
            return

        def done(t: asyncio.Task[Any]) -> None:
            title = live.stored.title or live.session_id[:8]
            if t.cancelled():
                text, level = f"Background session '{title}' was stopped.", "warning"
            elif live.needs_input:
                text, level = f"Background session '{title}' needs your input.", "warning"
            elif t.exception() is not None:
                text, level = f"Background session '{title}' failed: {t.exception()}", "error"
            elif (ended := _turn_status(t.result())) == "interrupted":  # a job catches the /stop and returns
                text, level = f"Background session '{title}' was stopped.", "warning"
            elif ended == "error":
                text, level = f"Background session '{title}' failed: {str(t.result()[1])[:160]}", "error"
            else:
                last = next((m.get("content") for m in reversed(live.messages) if m.get("role") == "assistant"), "")
                text, level = f"Background session '{title}' finished. {str(last or '')[:160]}".strip(), "info"
            target = self.live.get(notify_sid)
            payload = {
                "text": text,
                "level": level,
                "kind": "background",
                "key": f"bg-{live.session_id}",
                "session_id": live.session_id,
            }
            if target is not None:
                target.emit("notification.show", payload, importance="essential")
            else:
                self.emit("notification.show", payload, importance="essential")
            self.emit(
                "session.background_done",
                {"session_id": live.session_id, "state": live.state, "origin_session": notify_sid},
                importance="essential",
            )

        task.add_done_callback(done)

    async def interrupt_turn(self, session_id: str | None = None) -> bool:
        """Interrupt the running turn (session.interrupt, /stop)."""
        session = self._session_for(session_id)
        if session is None or session.turn_task is None or session.turn_task.done():
            return False
        session.pending_prompts.clear()  # /stop means stop: what was queued behind the turn does not run either
        session.steer_queue.clear()
        session.loop.interrupt() if session.loop else None
        self.subagents.interrupt_session(session.session_id)
        self.learning.record("interrupt", session, subject=_running_tool(session), choice="stop")
        if session.reliability is not None:
            session.reliability.cancel()  # abort a paused/parked wait too
        session.turn_task.cancel()
        return True

    def _session_for(self, session_id: str | None) -> LiveSession | None:
        if session_id:
            return self.live.get(session_id)
        return self.session

    # ── logging ───────────────────────────────────────────────────────

    def log(self, message: str) -> None:
        logger.info(message)


def _take_all(queue: list[str]) -> list[str]:
    """Empty ``queue`` in place and return what it held."""
    taken = queue[:]
    queue.clear()
    return taken


def _running_tool(session: Any) -> str:
    """Name of the tool the loop was in the middle of (last assistant tool call), else ``turn``."""
    for m in reversed(getattr(getattr(session, "loop", None), "turn_messages", None) or []):
        calls = getattr(m, "tool_calls", None)
        if getattr(m, "role", "") == "assistant" and calls:
            return str(calls[-1].name)
    return "turn"


def _has_verification(plan: str) -> bool:
    import re as _re

    m = _re.search(r"^\s*#{1,4}\s*Verification\s*\n(.*?)(?=^\s*#{1,4}\s|\Z)", plan or "", _re.S | _re.M | _re.I)
    return bool(m and m.group(1).strip())


class ApprovalTimeout(RuntimeError):
    """A server→client request (approval, plan, clarify) went unanswered past ``approval_timeout_s``."""


class _InvalidParams(Exception):
    """Raised by handlers for schema-invalid params (JSON-RPC -32602)."""


def _resolve_model_specs(config: Any, key: str) -> list[str | list[str]]:
    """Per-provider model specs for ``key`` (same fallback logic as the CLI)."""
    resolved: list[str | list[str]] = []
    for p in config.providers:
        spec = p.models.get(key)
        if spec is None:
            spec = p.models.get("default")
        if spec is None:
            spec = next(iter(p.models.values()), "")
        resolved.append(spec)
    return resolved


def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        entry: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_call_id:
            entry["tool_call_id"] = m.tool_call_id
        if m.name:
            entry["name"] = m.name
        if m.tool_calls:
            entry["tool_calls"] = [_stored_tool_call(tc) for tc in m.tool_calls]
        out.append(entry)
    return out


#: What the model reads for a tool call whose turn ended before the result was stored.
INTERRUPTED_RESULT = (
    "[interrupted] This call was cut off before it returned a result (the turn was stopped, or the daemon restarted "
    "while it ran). It may have run completely, partly or not at all: check the state before you repeat it."
)


def _answer_interrupted_calls(history: list[Message]) -> list[Message]:
    """``history`` with a result inserted after every assistant tool call that has none.

    Providers need a result for each call, so ``normalize_tool_pairs`` used to drop the unanswered call from the request
    without a word. The model then never learned that a command had been started (a `git push`, a migration) and often
    ran it again; the journal knows the call was interrupted, the history now says so too."""
    answered = {m.tool_call_id for m in history if m.role == "tool"}
    if all(tc.id in answered for m in history if m.role == "assistant" for tc in m.tool_calls):
        return history
    out: list[Message] = []
    for m in history:
        out.append(m)
        if m.role == "assistant":
            out.extend(
                Message(role="tool", content=INTERRUPTED_RESULT, tool_call_id=tc.id, name=tc.name)
                for tc in m.tool_calls
                if tc.id not in answered
            )
    return out


def _stored_tool_call(tc: ToolCall) -> dict[str, Any]:
    """A tool call as stored. ``raw_arguments`` (the model's own argument text) is kept: a turn rebuilt from storage
    must re-send the same bytes as the turn that made the call, or the provider's prompt cache misses from there."""
    stored: dict[str, Any] = {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
    if tc.raw_arguments is not None:
        stored["raw_arguments"] = tc.raw_arguments
    return stored


async def _change_preview(tool_name: str, arguments: dict[str, Any], cwd: Path) -> str | None:
    """The approval prompt's diff for a write/edit (see tools.preview), read off the event loop; None for other tools
    and whenever it cannot be made: a preview must never stand in the way of the prompt itself."""
    if tool_name not in ("write", "edit"):
        return None
    from k3code.tools.preview import change_preview

    try:
        return await asyncio.to_thread(change_preview, tool_name, arguments, cwd)
    except Exception:  # noqa: BLE001
        logger.warning("approval preview failed for %s", tool_name, exc_info=True)
        return None


def _command_for_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """Human-readable command line shown in the approval prompt."""
    if tool_name == "bash":
        return str(arguments.get("command", ""))
    if tool_name in ("write", "edit"):
        return f"{tool_name} {arguments.get('path', '')}"
    if tool_name == "read":
        return f"read {arguments.get('path', '')}"
    return f"{tool_name} {json.dumps(arguments, ensure_ascii=False)[:120]}"


def _file_keys(base: Path) -> set[str]:
    """Top-level keys of the user and project config files (none when a file is absent or unreadable)."""
    keys: set[str] = set()
    for path in (_user_cfg(), _proj_cfg(base)):
        try:
            keys |= set(confio.read_yaml(path))
        except confio.ConfigError:
            continue
    return keys


def _usage_payload(usage: Usage) -> dict[str, Any]:
    """The ``usage`` of ``session.usage`` and ``message.complete``. The cache counts are part of prompt_tokens."""
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.prompt_tokens + usage.completion_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_creation_tokens,
    }


# ── M1-core method handlers ──────────────────────────────────────────────


def _norm_cwd(cwd: str | None) -> str | None:
    """One spelling per directory (symlinks resolved, no trailing slash), so clients can compare cwds for equality."""
    return os.path.realpath(cwd) if cwd else None


_MAX_PASTE_SPANS = 10_000


def _paste_spans(params: dict[str, Any], text: Any) -> tuple[tuple[int, int], ...]:
    """``paste_spans`` of prompt.submit / session.steer: ``[start, end]`` code-point offsets of pasted text in ``text``.

    Optional (absent or null: nothing pasted); anything malformed is refused rather than guessed at."""
    raw = params.get("paste_spans")
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > _MAX_PASTE_SPANS:
        raise _InvalidParams(f"paste_spans must be a list of at most {_MAX_PASTE_SPANS} [start, end] pairs")
    size = len(str(text))
    spans: list[tuple[int, int]] = []
    for span in raw:
        ok = isinstance(span, list) and len(span) == 2
        ok = ok and all(isinstance(n, int) and not isinstance(n, bool) for n in span)
        if not ok or not 0 <= span[0] <= span[1] <= size:
            raise _InvalidParams(f"paste_spans: bad span {span!r} (want [start, end], 0 <= start <= end <= {size})")
        spans.append((span[0], span[1]))
    return tuple(spans)


def _require(params: dict[str, Any], key: str) -> Any:
    value = params.get(key)
    if value is None or value == "":
        raise _InvalidParams(f"missing required param: {key}")
    return value


async def _session_create(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    ultra = params.get("ultra_mode")
    if ultra is not None and ultra not in tune_cmd.ULTRA_MODES:
        raise _InvalidParams(f"unknown ultra_mode: {ultra} (one of {', '.join(tune_cmd.ULTRA_MODES)})")
    for key in ("cwd", "model", "provider"):
        if params.get(key) is not None and not isinstance(params[key], str):
            raise _InvalidParams(f"{key} must be a string")
    stored = server.store.create(
        model=params.get("model") or server.config.default_model,
        provider=params.get("provider") or "",
        cwd=_norm_cwd(params.get("cwd")) or str(Path.cwd()),
    )
    if params.get("background"):
        stored.meta["background"] = True
        server.store.save(stored)
    if ultra == "ultracode":  # kept in the meta, so a resume and a /fork of this session have it
        stored.meta["ultra_mode"] = ultra
        server.store.save(stored)
    try:
        live = LiveSession(stored.session_id, stored, server)
    except Exception:
        server.store.delete(stored.session_id)  # e.g. a bad permission_mode: each attempt left an empty row behind
        raise
    live.reasoning_effort = params.get("effort")
    server.session = live
    if server.learning.enabled and not params.get("background"):
        server.learning.spawn(server.learning.prepare_project(live))
    return {"session_id": stored.session_id, "info": live.live_info()}


#: Most stored sessions ``session.list {cwd}`` considers looking for that project's (the newest non-empty,
#: non-automation ones; older history of a project buried under this many newer sessions elsewhere is not listed).
#: Old rows can spell the cwd with a symlink or trailing slash, so the match is made in Python, not with
#: ``WHERE cwd = ?``; only ids and cwds are scanned, full rows are loaded for the matches alone.
_SESSION_LIST_SCAN_CAP = 2000


def _project_sessions(server: GatewayServer, cwd: str, limit: int) -> list[SessionSummary]:
    """The ``limit`` newest non-empty, non-automation sessions whose normalised cwd is ``cwd`` (already normalised)."""
    spelled: dict[str, str | None] = {}  # one realpath per distinct stored spelling
    ids: list[str] = []
    for sid, stored_cwd in server.store.worked_in(limit=_SESSION_LIST_SCAN_CAP):
        if stored_cwd not in spelled:
            spelled[stored_cwd] = _norm_cwd(stored_cwd)
        if spelled[stored_cwd] == cwd:
            ids.append(sid)
            if len(ids) >= limit:
                break
    return server.store.summaries(ids)


async def _session_list(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``cwd`` (optional) narrows the list to that project's earlier sessions: same normalised cwd, at least one
    message, no automation runs, ``limit`` applied after the filter. Without it: the newest ``limit`` of all."""
    try:
        limit = int(params.get("limit") or 50)
    except (TypeError, ValueError):
        raise _InvalidParams("limit must be a whole number") from None
    if params.get("cwd") is not None and not isinstance(params["cwd"], str):
        raise _InvalidParams("cwd must be a string")
    cwd = _norm_cwd(params.get("cwd"))
    sessions = _project_sessions(server, cwd, max(1, limit)) if cwd else server.store.list(limit=limit)
    rows = []
    for s in sessions:
        preview = s.preview
        live = server.live.get(s.session_id)
        state = live.state if live is not None else ("completed" if s.message_count else "new")
        rows.append(
            {
                "id": s.session_id,
                "title": s.title or preview or "Session",
                "preview": preview,
                "started_at": s.created_at,
                "message_count": s.message_count,
                "status": state,
                "state": state,
                "cwd": _norm_cwd(s.cwd),
            }
        )
    return {"sessions": rows}


async def _session_active_list(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    current = server.session
    out: dict[str, Any] = {"sessions": server._active_rows(current.session_id if current else None)}
    if server.automation is not None:
        counts = server.automation.counts()
        out["automation"] = {**counts, "active": sum(counts.values())}
    return out


_RECORD_EXIT = re.compile(r"\[exit code (-?\d+)\]\Z")


def _record_code(output: str) -> int:
    """The exit code a `!cmd` record shows: its ``[exit code N]`` line, 130 cancelled, 124 timed out, else 0."""
    if hit := _RECORD_EXIT.search(output):
        return int(hit.group(1))
    if output.endswith("[cancelled by the user]"):
        return 130
    return 124 if "\n[timed out after " in f"\n{output}" else 0


def transcript_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stored (OpenAI-shaped) messages -> the ``{role, text, name, context}`` rows the TUI renders on resume.

    System prompts and assistant tool-call stubs are dropped; a tool result becomes a ``tool`` row named after
    the call that produced it.
    """
    names: dict[str, tuple[str, str]] = {}
    rows: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):  # a hand-edited or imported row: skip the entry, keep the resume
            continue
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                names[str(tc.get("id"))] = (
                    str(tc.get("name") or fn.get("name") or "tool"),
                    str(tc.get("arguments") or fn.get("arguments") or "")[:80],
                )
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        if role == "tool":
            name, ctx = names.get(str(m.get("tool_call_id")), (str(m.get("name") or "tool"), ""))
            rows.append({"role": "tool", "name": name, "context": ctx})
        elif is_shell_record(m):  # a `!cmd`: the command as the user typed it, then its output as the TUI shows it
            command, output = shell_record_parts(str(content))
            rows.append({"role": "user", "text": f"!{command}"})
            rows.append(
                {
                    "role": "system",
                    "display_kind": "shell",
                    "text": output,
                    "display_metadata": {"command": command, "code": _record_code(output)},
                }
            )
        elif role in ("user", "assistant") and isinstance(content, str) and content.strip():
            rows.append({"role": role, "text": content})
    return rows


async def _session_resume(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    stored = server.store.get(str(sid))
    if stored is None:
        raise _InvalidParams(f"unknown session: {sid}")
    live = server.live_for(stored)  # a background session keeps its running state
    server.live[live.session_id] = live
    server.attach(_ctx_client.get() or server._stdio_client, live, replay_delay=0.15)
    server.emit("session.resume_progress", {"phase": "done", "status": "done", "message_count": len(stored.messages)})
    return {
        "session_id": stored.session_id,
        "message_count": len(live.stored.messages),
        "messages": transcript_rows(live.stored.messages),
        "info": live.live_info(),
        # like session.activate: attaching to (or reconnecting into) a session mid-turn must show it busy, or the
        # composer looks idle and the next prompt is typed into a running turn
        "running": live.streaming,
        "status": live.state,
    }


async def _session_activate(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    stored = server.store.get(str(sid))
    if stored is None:
        raise _InvalidParams(f"unknown session: {sid}")
    live = server.live_for(stored)
    server.live[live.session_id] = live
    server.attach(_ctx_client.get() or server._stdio_client, live, replay_delay=0.15)
    return {
        "session_id": stored.session_id,
        "info": live.live_info(),
        "status": live.state,
        "running": live.streaming,
        "messages": transcript_rows(live.stored.messages),
    }


async def _session_close(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """The TUI closes the session it just left (``/resume``, new session). An idle foreground session is dropped from
    the live registry (its stored copy stays resumable); one that is running, backgrounded or waiting for an
    answer keeps going, so it stays in the agent strip.

    ``disposable_only`` (sent after the caller attached elsewhere) closes only an empty, idle session no loop or
    automation is bound to (its stored row stays); anything else is left alone with ``{closed: false, reason}``, not
    an error. A caller still on the session counts as using it either way."""
    return await server.close_live(
        str(params.get("session_id") or ""), disposable_only=bool(params.get("disposable_only"))
    )


async def _session_delete(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    deleted = server.store.delete(str(sid))
    live = server.live.pop(str(sid), None)
    await tool_jobs.reap(str(sid))  # background bash jobs end with their session
    if live is not None:
        if live.turn_task is not None and not live.turn_task.done():
            live.turn_task.cancel()
        if live.reliability is not None:
            await live.reliability.stop()  # closes the journal file before it goes
    server.remove_session_files(str(sid))
    for client in server.clients:
        if client.session_id == sid:
            client.session_id = None
    return {"deleted": deleted}


async def _session_title(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    title = scrub_surrogates(str(_require(params, "title")))
    # The live session's own record: saving a detached copy of the row was undone by the next save of the live one,
    # and an auto title could overwrite it.
    live = server.live.get(str(sid))
    stored = live.stored if live is not None else server.store.get(str(sid))
    if stored is None:
        raise _InvalidParams(f"unknown session: {sid}")
    stored.title = str(title)
    server.store.save_meta_of(stored)
    server.emit("session.title", {"session_id": str(sid), "title": stored.title})
    return {"ok": True, "title": stored.title}


async def _session_interrupt(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    interrupted = await server.interrupt_turn(params.get("session_id"))
    return {"interrupted": interrupted}


async def _session_steer(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    text = scrub_surrogates(str(_require(params, "text")))
    pasted = _paste_spans(params, text)
    session = server.session
    if session is None or not session.streaming:
        return {"steered": False, "status": "rejected"}
    # The running loop adds it before its next model call. Appending to stored.messages lost it: the loop never saw
    # it and the turn's persist overwrote the list. Typed text keeps its marker: if no loop takes it, it runs as the
    # next prompt, wake words and all (``automated`` is the TUI's own text, see _prompt_submit).
    typed = TypedPrompt(text, pasted)
    session.steer_queue.append(str(text) if params.get("automated") is True else typed)
    # `status` is what the TUI checks: without "queued" it queued the text again for the next turn, so a successful
    # steer reached the model twice (once into the running turn, once as a new turn)
    return {"steered": True, "status": "queued"}


async def _session_control_read(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    session = server.session
    empty = {"goal": "", "loop": "", "heartbeat": "", "revision": 0, "updated_at": 0.0}
    return {"control": dict(session.control) if session else empty}


async def _session_control(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    session = server.session
    if session is None:
        raise _InvalidParams("no active session")
    for key in ("goal", "loop", "heartbeat"):
        if key in params and params[key] is not None:
            session.control[key] = params[key]
    session.control["revision"] += 1
    session.control["updated_at"] = time.time()
    server.emit("session.control.update", {"control": dict(session.control)})
    return {"ok": True}


async def _session_workspace_move(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(_require(params, "path"))).expanduser().resolve()
    if not path.is_dir():
        raise _InvalidParams(f"not a directory: {path}")
    session = server.session
    if session is None:
        raise _InvalidParams("no active session")
    session.stored.cwd = str(path)
    session.perms.cwd = path  # the permission state keeps the old directory otherwise (project rules, sandbox root)
    session.perms.reload()
    session.refresh_prompt_inputs()  # the new project's notes, not the old one's, are shown from the next prompt on
    server.store.save(session.stored)
    return {"ok": True, "cwd": str(path)}


async def _session_most_recent(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    stored = server.store.most_recent()
    if stored is None:
        return {"session_id": None}
    return {"session_id": stored.session_id, "info": {"title": stored.title, "message_count": len(stored.messages)}}


async def _session_events_since(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    _require(params, "session_id")
    return {"events": [], "latest_seq": 0, "truncated": False, "count": 0, "epoch": 1, "open_requests": []}


async def _session_events_stats(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    _require(params, "session_id")
    return {"total": 0, "by_type": {}}


async def _prompt_submit(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    # `session_id` stays ignored: the TUI always sends its attached sid, and honouring it would move a send made
    # during a session switch to the session being left. The agent view names another live session explicitly.
    session = server.session
    tgt = params.get("target_session_id")
    if tgt is not None:
        if not isinstance(tgt, str):
            raise _InvalidParams("target_session_id must be a string")
        session = server.live.get(tgt)
        if session is None:
            raise _InvalidParams(f"session {tgt} is not running; attach it first")
    if session is None:
        raise _InvalidParams("no active session")
    text = scrub_surrogates(str(_require(params, "text")))  # one lone surrogate failed every save of the session
    if server.halted:
        raise _InvalidParams("daemon is halted (/daemon pause); resume with /daemon resume")
    server.last_user_activity = time.time()
    # `automated`: text the TUI generated (a /skill expansion, the /go send, an accepted proposal), not typed by the
    # user. Only typed text is checked for wake words and the ultracode mode, now or when it drains from the queue.
    prompt = str(text) if params.get("automated") is True else TypedPrompt(text, _paste_spans(params, text))
    if session.turn_in_flight:
        # really queued: it runs when the current turn ends. The task check covers a turn that has not reached
        # `streaming = True` yet (compaction, MCP start): a second task there overwrote turn_task, so /stop missed one.
        session.pending_prompts.append(prompt)
        return {"turn_id": "", "status": "queued"}
    model = params.get("model")
    if model and (not isinstance(model, str) or model not in tune_cmd.known_model_keys(server.config)):
        raise _InvalidParams(tune_cmd.unknown_model_message(server.config, str(model)))  # a typo is not stored
    if params.get("background"):
        session.background = True
        session.stored.meta["background"] = True
    if session.background and server.background_paused:
        raise _InvalidParams("background work is paused (restart-storm safe mode); resume with /daemon resume")
    session.stored.model = model or session.stored.model
    session.turn_task = asyncio.get_running_loop().create_task(server._run_turn(session, prompt))
    return {"turn_id": session.turn_task.get_name(), "status": "streaming"}


async def _prompt_background(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``/bg``/Ctrl+B. With ``text``: new background session. Without: hand the running turn off."""
    session = server._session_for(params.get("session_id")) or server.session
    if session is None:
        raise _InvalidParams("no active session")
    text = str(params.get("text") or "").strip()
    if text:
        live = server.start_background(session, text, cwd=params.get("cwd"))
        return {"session_id": live.session_id, "status": "started", "info": live.live_info()}
    if not session.streaming or session.turn_task is None or session.turn_task.done():
        raise _InvalidParams("nothing is running in this session; give a prompt: /bg <prompt>")
    fresh = server.background_current(session, _ctx_client.get())
    return {
        "session_id": session.session_id,
        "new_session_id": fresh.session_id,
        "status": "backgrounded",
        "info": fresh.live_info(),
    }


async def _subagent_list(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = params.get("session_id") or (server.session.session_id if server.session else "")
    rows = [
        {
            "subagent_id": h.id,
            "parent_id": h.parent_child_id,
            "depth": h.depth - 1,
            "goal": h.description,
            "model": h.model or h.tier,
            "started_at": h.started_wall,
            "status": h.status,
            "tool_count": h.tool_count,
            "last_tool": h.last_tool,
        }
        for h in server.subagents.for_session(sid)
    ]
    return {"subagents": rows, "delegations": []}


async def _subagent_interrupt(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    hid = str(_require(params, "subagent_id"))
    return {"found": server.subagents.interrupt(hid), "subagent_id": hid}


TAIL_LINES = 30


async def _subagent_tail(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    h = server.subagents.handles.get(str(_require(params, "subagent_id")))
    text = "\n".join(h.tail[-TAIL_LINES:]) + (("\n" + h.result) if h and h.done else "") if h else ""
    return {
        # the TUI's live view shows "unavailable" unless this is true: a known child always has a transcript to show
        "available": h is not None,
        "truncated": bool(h and len(h.tail) > TAIL_LINES),
        "text": text,
        "status": h.status if h else "unknown",
        "done": bool(h and h.done),
    }


async def _clipboard_paste(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    # M1: no clipboard integration; the TUI pastes text inline instead.
    return {"text": "", "images": [], "files": []}


async def _process_list(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """The TUI's process dock (polled): the session's running foreground ``bash`` commands and its background jobs.

    In memory only (no disk, no subprocess): it is asked every 1.5 s. Each entry has the TUI's ``ProcessEntry`` shape
    plus ``pid``, ``started``, ``cwd`` and ``kind``; its ``session_id`` is the process's own id (the TUI keys rows on
    it), the owning session is the one asked for."""
    live = server._session_for(params.get("session_id"))
    return {"processes": tool_jobs.REGISTRY.processes(live.session_id) if live is not None else []}


#: Pasted text the TUI collapsed into a token is kept here, one directory per session (removed with the session).
PASTES_DIR = "pastes"


async def _paste_collapse(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """Store a large paste the TUI shows as a ``[Pasted text #N]`` token; returns ``{path}`` of the stored copy.

    The file name is the content hash, so pasting the same text twice writes one file. The session is the caller's
    live session (the TUI sends no session id); only a live session's id ever becomes a directory name."""
    text = params.get("text")
    if not isinstance(text, str):
        raise _InvalidParams("text must be a string")
    live = server.live.get(str(params.get("session_id") or "")) or server.session
    sid = live.session_id if live is not None else "unbound"
    folder = server._home() / PASTES_DIR / sid
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = text.encode("utf-8")
    path = folder / f"{hashlib.sha256(data).hexdigest()[:16]}.txt"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    return {"path": str(path), "chars": len(text), "lines": text.count("\n") + 1}


async def _image_attach(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    raise _InvalidParams("image.attach is not supported in M1 (text-only providers)")


async def _image_detach(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    return {"detached": 0, "count": 0}


async def _input_detect_drop(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    paths = params.get("paths") or []
    return {"images": [], "files": [str(p) for p in paths if p]}


async def _command_dispatch(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    name = str(_require(params, "name")).lstrip("/")
    arg = str(params.get("arg") or "")
    return await server.dispatch_command(name, arg, params.get("session_id"))


async def _slash_exec(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    # The TUI sends the command without its leading slash ("model foo"); accept both.
    command = str(_require(params, "command")).strip().lstrip("/")
    session_id = params.get("session_id")
    parts = command.split(None, 1)
    if not parts:
        raise _InvalidParams("command is empty: give a slash command name")
    name, arg = parts[0], parts[1] if len(parts) > 1 else ""
    token = _ctx_cwd.set(params.get("cwd") or None)
    try:
        return await server.dispatch_command(name, arg, session_id)
    finally:
        _ctx_cwd.reset(token)


async def _model_options(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    providers = []
    for p in server.config.providers:
        providers.append(
            {
                "slug": p.name,
                "name": p.name,
                "models": sorted(p.models.keys()),
                "api_url": p.base_url,
            }
        )
    # the model key of the session asked about (else the current one), not only the configured default
    session = server._session_for(params.get("session_id"))
    return {"providers": providers, "model": tune_cmd.current_model(server.config, session)}


async def _tune_get(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``tune.get``: what the tune popup shows (models, effort levels, ultracode mode, wake words)."""
    return tune_cmd.snapshot(server, server._session_for(params.get("session_id")))


async def _tune_set(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``tune.set {session_id?, model?, effort?, ultra_mode?, scope?}``: validate all, apply all or nothing."""
    session = server._session_for(params.get("session_id"))
    try:
        outcome = tune_cmd.apply_tune(server, session, tune_cmd.request_from_params(params))
    except tune_cmd.TuneError as e:
        raise _InvalidParams(str(e)) from None
    return {**tune_cmd.snapshot(server, session), "ok": True, "changed": outcome.changed}


async def _model_save_key(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """The picker's "add a key" step. k3code keeps no API keys (they are secrets: they come from the environment
    variable a provider entry names), so this answers with what to do instead of the "missing provider" a mismatch of
    parameter names gave. The key sent is never read, logged or echoed."""
    slug = str(params.get("slug") or params.get("provider") or "")
    entry = next((p for p in server.config.providers if p.name == slug), None)
    env = getattr(entry, "api_key_env", "") if entry is not None else ""
    where = (
        f"set {env} in the environment of the daemon" if env else "name an api_key_env in the provider's config entry"
    )
    raise _InvalidParams(f"k3code does not store API keys: {where}, then restart it (k3code daemon restart)")


async def _model_disconnect(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """Nothing to forget: no key is stored (see _model_save_key); unset the environment variable to disconnect."""
    return {"disconnected": False}


async def _mode_session(server: GatewayServer, params: dict[str, Any]) -> LiveSession:
    session = server._session_for(params.get("session_id"))
    if session is None:
        raise _InvalidParams("no active session")
    return session


async def _session_mode_cycle(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    session = await _mode_session(server, params)
    session.set_mode(session.perms.cycle_mode())
    return {"mode": session.perms.mode.value, "info": session.live_info()}


async def _session_mode_set(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    session = await _mode_session(server, params)
    raw = str(_require(params, "mode"))
    try:
        mode = PermissionMode(raw)
    except ValueError:
        raise _InvalidParams(f"unknown mode: {raw} (one of {', '.join(MODE_CYCLE_NAMES)}, yolo)") from None
    session.set_mode(mode)
    return {"mode": mode.value, "info": session.live_info()}


async def _config_get(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    key = str(_require(params, "key"))
    if key == "mtime":  # the TUI polls this to notice an edited config.yaml; a constant 0 made hot reload dead
        newest = 0.0
        for path in (_user_cfg(), _proj_cfg(default_project_dir())):
            with contextlib.suppress(OSError):
                newest = max(newest, path.stat().st_mtime)
        return {"mtime": newest}
    if key == "focus_view":  # the TUI reads display.focus_mode (setup: "Focus mode on by default?") under this name
        return {"value": "1" if server.config.display.focus_mode else "0"}
    if tui_display.handles(key):
        return tui_display.get(server.config.display, key)
    # Every key is looked up in the redacted JSON dump: provider api_key, mcp server env/headers never go to clients,
    # and a section ("providers", "mcp.servers") is plain JSON. Before, a sub-key returned the pydantic object (the
    # reply failed to encode and never came) or a secret unredacted ("mcp.servers.x.headers").
    value: Any = redact(server.config.model_dump(mode="json"))
    if key == "full":
        return {"config": value}
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            break
    return {"value": value}


async def _config_set(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    key = str(_require(params, "key"))
    if key == "yolo":  # TUI /yolo toggle
        session = await _mode_session(server, params)
        yolo = session.perms.mode != PermissionMode.YOLO
        session.set_mode(PermissionMode.YOLO if yolo else PermissionMode.DEFAULT)
        return {"ok": True, "key": key, "value": "1" if yolo else "0"}
    if key in ("focus", "display.focus"):  # /focus: display-only, stored as display.focus_mode
        raw = params.get("value")
        on = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "on", "yes")
        server.config.display.focus_mode = on
        tui_display._persist("display.focus_mode", on)  # saved like the other look-and-feel choices
        return {"ok": True, "key": key, "value": "on" if server.config.display.focus_mode else "off"}
    if key == "model":  # TUI /model <key> and the model picker: switch the session's model key
        return await _config_set_model(server, params)
    if key == "reasoning":  # TUI /reasoning show|hide|<level>
        return await _config_set_reasoning(server, params)
    if tui_display.handles(key):  # TUI /theme /indicator /statusbar /battery /pet ...: saved to display.*
        try:
            return tui_display.set_(server.config.display, key, params.get("value"))
        except tui_display.DisplayValueError as e:
            raise _InvalidParams(f"{key}: {e}") from None
    if key not in SETTABLE_CONFIG_KEYS:
        # Before, any section.field was setattr'd with the raw value: a dict section matched its methods
        # ("permissions.update"), and a pydantic field took any type (goal.max_turns = "lots").
        raise _InvalidParams(f"unsupported config key: {key} (settable: {', '.join(sorted(SETTABLE_CONFIG_KEYS))})")
    section, field_name = key.split(".", 1)
    section_obj = getattr(server.config, section)
    annotation = type(section_obj).model_fields[field_name].annotation
    try:
        value = TypeAdapter(annotation).validate_python(params.get("value"))
    except ValidationError as e:
        raise _InvalidParams(f"{key}: {e.errors()[0].get('msg', 'invalid value')}") from None
    setattr(section_obj, field_name, value)
    return {"ok": True, "key": key, "value": value}


#: ``section.field`` keys the generic ``config.set`` path may change at runtime (each value validated against the
#: field's type). Display keys, ``model``, ``reasoning`` and ``yolo`` have their own branches above.
SETTABLE_CONFIG_KEYS = frozenset(
    {
        "display.theme",
        "display.focus_mode",
        "goal.max_turns",
        "goal.gate_max_retries",
        "goal.judge_model",
    }
)


async def _config_set_reasoning(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``config.set reasoning``: ``show``/``hide`` toggles the thinking display (saved); a level sets /effort."""
    words = [w for w in str(params.get("value") or "").lower().split() if not w.startswith("--")]
    value = words[0] if words else ""
    if value in ("show", "on", "hide", "off"):
        show = value in ("show", "on")
        server.config.display.show_reasoning = show  # type: ignore[attr-defined]  # DisplayConfig allows extras
        tui_display._persist("display.show_reasoning", show)
        return {"ok": True, "key": "reasoning", "value": "show" if show else "hide"}
    if not value:  # bare /reasoning: report the session's effort
        session = server._session_for(params.get("session_id"))
        return {"ok": True, "key": "reasoning", "value": (session and session.reasoning_effort) or "default"}
    if value not in (*effort_mod.LEVELS, "default"):
        raise _InvalidParams(f"reasoning: expected show, hide or one of {', '.join(effort_mod.LEVELS)}, default")
    live = await _mode_session(server, params)
    live.reasoning_effort = None if value == "default" else value
    if live.reasoning_effort is None:
        live.stored.meta.pop("reasoning_effort", None)
    else:
        live.stored.meta["reasoning_effort"] = live.reasoning_effort
    server.store.save_meta_of(live.stored)
    live.emit("session.info", live.live_info())
    return {"ok": True, "key": "reasoning", "value": value}


async def _config_set_model(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``config.set model``: value is ``<model-key> [--provider p] [--reasoning <level>] [--session|--global]``.

    ``--global`` also makes the key the saved default of new sessions; ``--reasoning`` sets the session's effort
    (ignored without a session); ``--provider`` and the other picker flags are ignored."""
    parts = iter(str(params.get("value") or "").split())
    keys: list[str] = []
    reasoning: str | None = None
    scope = "session"
    for part in parts:
        if part == "--provider":
            next(parts, None)  # its value
        elif part == "--reasoning":
            reasoning = next(parts, "")
        elif part == "--global":
            scope = "default"
        elif not part.startswith("--"):
            keys.append(part)
    if not keys:
        raise _InvalidParams("model key required")
    key = keys[0]
    known = {m for p in server.config.providers for m in p.models} | {server.config.default_model}
    if key not in known:
        raise _InvalidParams(f"unknown model key: {key} (known: {', '.join(sorted(known))})")
    session = server._session_for(params.get("session_id"))
    try:
        effort = tune_cmd.normalize_effort(reasoning, legacy=True) if reasoning is not None and session else None
        tune_cmd.apply_tune(server, session, tune_cmd.TuneRequest(model=key, effort=effort, scope=scope))
    except tune_cmd.TuneError as e:
        raise _InvalidParams(str(e)) from None
    return {"ok": True, "key": "model", "value": key}


async def _setup_status(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_configured": bool(server.config.providers),
        "ready": bool(server.config.providers),
        "version": __version__,  # doctor compares it with the installed `current` to spot a stale daemon
    }


async def _system_battery(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    return {"available": False}


async def _spawn_tree_save(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """The TUI offers each finished sub-agent tree for archiving after the turn. The core keeps the sub-agent runs in
    ``subagent.list`` / the session transcript and nothing reads a second copy, so it is acknowledged, not stored (an
    unknown-method error per finished turn was wasted traffic)."""
    return {"ok": True, "persisted": False}


def _session_dir(server: GatewayServer, params: dict[str, Any]) -> Path:
    """The directory completions resolve against: the session's cwd, else the process cwd."""
    try:
        live = server.live.get(params.get("session_id")) if params.get("session_id") else None
        if live is not None and getattr(live.stored, "cwd", ""):
            return Path(live.stored.cwd)
    except Exception:  # noqa: BLE001 - completion must never fail the composer
        pass
    return Path.cwd()


async def _complete_slash(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """Slash-command completion for the composer; ``text`` is the input, starting with ``/``."""
    text = str(params.get("text", ""))
    if not text.startswith("/") or " " in text:
        return {"items": [], "replace_from": 1}
    prefix = text[1:].lower()
    items: list[dict[str, str]] = []
    for name in server.commands.names():
        if name.lower().startswith(prefix):
            cmd = server.commands.get(name)
            items.append(
                {"text": f"/{name}", "display": f"/{name}", "meta": (cmd.help if cmd else "") or "", "kind": "command"}
            )
    try:  # skills are offered too (kind="skill"); best effort
        from k3code.skills import discover

        for sk in discover(_session_dir(server, params), getattr(server.config.skills, "roots", None)):
            if sk.name.lower().startswith(prefix):
                items.append(
                    {"text": f"/{sk.name}", "display": f"/{sk.name}", "meta": sk.description or "", "kind": "skill"}
                )
    except Exception:  # noqa: BLE001 - skills are optional
        pass
    return {"items": items[:50], "replace_from": 1}


async def _complete_path(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """Path completion for ``@file`` / ``./x`` words, relative to the session's directory."""
    word = str(params.get("word", ""))
    raw = word.lstrip("@")
    try:
        expanded = Path(raw).expanduser()
    except RuntimeError:  # `~nouser/x`: no such home directory
        return {"items": []}
    directory = expanded if raw.endswith("/") else expanded.parent
    if not directory.is_absolute():
        directory = _session_dir(server, params) / directory
    stem = "" if raw.endswith("/") else expanded.name
    items: list[dict[str, str]] = []
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith(".") and not stem.startswith("."):
                continue
            if not entry.name.lower().startswith(stem.lower()):
                continue
            shown = raw[: len(raw) - len(stem)] + entry.name + ("/" if entry.is_dir() else "")
            items.append(
                {
                    "text": ("@" if word.startswith("@") else "") + shown,
                    "display": shown,
                    "meta": "dir" if entry.is_dir() else "file",
                    "kind": "path",
                }
            )
            if len(items) >= 50:
                break
    except OSError:
        pass
    return {"items": items}


async def _browser_manage(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """Inspect, attach to the configured ``browser.cdp_url``, or drop the browser the browser tools use."""
    action = str(params.get("action") or "status").strip().lower()
    if action not in ("status", "connect", "disconnect"):
        raise _InvalidParams(f"browser.manage action must be status, connect or disconnect, not {action!r}")
    url = params.get("url")
    return await server.browser.manage(action, url=str(url) if url else None)


# ── TUI conveniences the Ink UI calls directly (each used to answer "Method not found", shown as "out of sync") ──

#: shell.exec: the longest command accepted, and what an exec_id may be (the client picks it to match shell.output)
MAX_SHELL_COMMAND_BYTES = 32 * 1024
_EXEC_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


async def _shell_exec(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """`!cmd` in the composer and `{!cmd}` inside a prompt: run the user's own command in the session's directory.

    Not sandboxed and not approved (the user typed it, as in a terminal), but the hardline rules, including the
    user's own ``permissions.hardline`` patterns, still refuse it (code 126, not run). A concurrent method: other
    frames of the client are answered while it runs, and ``shell.cancel`` stops it (code 130; 124 on a timeout) with
    the reply still sent. ``stream: true`` sends the output as ``shell.output`` events while it runs."""
    from k3code.permissions import hardline
    from k3code.tools.usershell import ShellStreamer, run_user_command

    command = _require(params, "command")
    if not isinstance(command, str):
        raise _InvalidParams("command must be a string")
    if len(command.encode("utf-8")) > MAX_SHELL_COMMAND_BYTES:
        raise _InvalidParams(f"command is longer than {MAX_SHELL_COMMAND_BYTES // 1024} KB")
    exec_id = params.get("exec_id") or f"x{uuid.uuid4().hex[:12]}"
    if not isinstance(exec_id, str) or not _EXEC_ID.fullmatch(exec_id):
        raise _InvalidParams("exec_id must be 1 to 64 of A-Z, a-z, 0-9, _ and -")
    sid = params.get("session_id")
    session = server._session_for(sid)
    if session is None:
        raise _InvalidParams(f"unknown session: {sid}" if sid else "no active session: start or resume one first")
    if exec_id in session.user_shells:
        raise _InvalidParams(f"exec_id {exec_id} is already running")
    cwd = Path(session.perms.cwd)
    reply: dict[str, Any] = {"exec_id": exec_id, "cwd": str(cwd), "recorded": False}
    if denied := hardline.check(command, list(session.perms.hardline_extra), cwd=str(cwd)):
        return {"code": 126, "stdout": "", "stderr": f"denied by hardline rule: {denied}", **reply}
    cancel = asyncio.Event()
    session.user_shells[exec_id] = cancel
    streamer = pacer = None
    if params.get("stream"):
        streamer = ShellStreamer(lambda payload: session.emit("shell.output", {"exec_id": exec_id, **payload}))
        pacer = asyncio.ensure_future(streamer.run())
    try:
        run = await run_user_command(
            command,
            cwd=cwd,
            session_id=session.session_id,
            cancel=cancel,
            on_output=streamer.feed if streamer is not None else None,
        )
    finally:
        session.user_shells.pop(exec_id, None)
        if pacer is not None:
            pacer.cancel()
    if streamer is not None:
        streamer.finish()
    code = run.exit_code if run.exit_code is not None else 1
    if run.cancelled:
        code = 130
    elif run.timed_out:
        code = 124
    elif code < 0:  # killed by a signal, as a shell reports it
        code = 128 - code
    stderr = run.stderr
    if run.error:
        stderr += ("\n" if stderr and not stderr.endswith("\n") else "") + run.error
    if params.get("record"):
        limit = tool_output_limit(server.config) or MAX_TOOL_RESULT_CHARS
        session.shell_records.append({"role": "user", "content": shell_record(command, run, limit)})
        if server._flush_shell_records(session):
            reply["recorded"] = "now"
        else:  # a turn runs: in at the next turn's start, else when this one ends
            reply["recorded"] = "after_turn"
            if (task := session.turn_task) is not None and not task.done():
                task.add_done_callback(lambda _t: server._flush_shell_records(session))
    return {
        "code": code,
        "stdout": run.stdout,
        "stderr": stderr,
        **reply,
        **({"cancelled": True} if run.cancelled else {}),
        **({"timed_out": True} if run.timed_out else {}),
    }


async def _shell_cancel(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """Stop `!cmd` commands: the one ``exec_id`` names, else every one of the session. Sets their cancel events (the
    runner kills the process group and shell.exec still replies); never cancels the shell.exec handler itself."""
    sid = params.get("session_id")
    session = server._session_for(sid)
    if session is None:
        raise _InvalidParams(f"unknown session: {sid}" if sid else "no active session: start or resume one first")
    exec_id = params.get("exec_id")
    events = [session.user_shells[exec_id]] if exec_id in session.user_shells else []
    if not exec_id:
        events = list(session.user_shells.values())
    for event in events:
        event.set()
    return {"cancelled": len(events)}


async def _session_undo(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """/undo and /retry: drop the last user message and everything after it."""
    session = server._session_for(params.get("session_id"))
    if session is None:
        raise _InvalidParams("no active session")
    if session.turn_in_flight:
        raise _InvalidParams("a turn is running in this session; /undo when it ends")
    messages = list(session.messages)
    # the last prompt, not a `!cmd` record: the records after it go with it, the ones before it stay
    last_user = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if messages[i].get("role") == "user" and not is_shell_record(messages[i])
        ),
        None,
    )
    if last_user is None:
        return {"removed": 0}
    session.messages = messages[:last_user]
    server.store.save(session.stored)
    return {"removed": len(messages) - last_user}


async def _session_usage(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """/usage: this session's model calls and tokens (``/stats`` covers every session)."""
    session = server._session_for(params.get("session_id"))
    if session is None:
        return {"calls": 0}
    rows = server.usage.aggregate(by="session", session=session.session_id)
    row = rows[0] if rows else {}
    t_in, t_out = int(row.get("tokens_in") or 0), int(row.get("tokens_out") or 0)
    cache_read, cache_write = int(row.get("cache_read") or 0), int(row.get("cache_write") or 0)
    return {
        "model": session.stored.model or server.config.default_model,
        "input": t_in,
        "output": t_out,
        "total": t_in + t_out,
        "calls": int(row.get("calls") or 0),
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cache_hit_pct": round(100 * cache_read / t_in) if t_in else 0,  # the share of prompt tokens read from cache
        **server.context_fields(session),
    }


async def _session_status(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """/status: the same view as /settings (model, tiers, providers, permissions, theme)."""
    from k3code.commands.settings_cmd import build_settings_view, render_text

    session = server._session_for(params.get("session_id"))
    return {"output": render_text(build_settings_view(server, session.session_id if session else None))}


async def _settings_get(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``settings.get``: the structured /settings view (plus its text) for the /dashboard Session and Status tabs."""
    from k3code.commands.settings_cmd import build_settings_view, render_text

    session = server._session_for(params.get("session_id"))
    view = build_settings_view(server, session.session_id if session else None)
    return {"settings": view, "output": render_text(view)}


async def _session_save(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """/save: write the session's transcript as JSON into its working directory."""
    session = server._session_for(params.get("session_id"))
    if session is None:
        raise _InvalidParams("no active session")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = Path(session.perms.cwd) / f"k3code-session-{session.session_id[:8]}-{stamp}.json"
    payload = {
        "session_id": session.session_id,
        "title": session.stored.title,
        "model": session.stored.model,
        "messages": list(session.messages),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"file": str(path)}


def _session_cwd(server: GatewayServer, params: dict[str, Any]) -> str | None:
    session = server._session_for(params.get("session_id"))
    return str(session.perms.cwd) if session is not None else None


async def _reload_mcp(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """/reload-mcp: re-read the config and restart the MCP servers (the gateway's `/mcp reload`)."""
    server.apply_file_config(_session_cwd(server, params))
    await server.mcp.reload(
        mcpjson.merged(server.config.mcp.servers, _session_cwd(server, params) or default_project_dir())
    )
    return {"status": "reloaded"}


async def _reload_env(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """/reload: re-read config.yaml and the key file (~/.config/k3code/env) into the running gateway."""
    from k3code.setup.state import read_env_file

    server.apply_file_config(_session_cwd(server, params))
    return {"updated": len(read_env_file())}


async def _skills_reload(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """/reload-skills: skill discovery is never cached, so this lists what a fresh scan finds."""
    res = await server.dispatch_command("skills", "", params.get("session_id"))
    return {"output": str(res.get("message") or res.get("output") or "")}


#: How /help groups the gateway's commands. A command missing here still shows up, under "Other".
HELP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Session",
        (
            "clear",
            "compact",
            "autocompact",
            "rename",
            "resume",
            "fork",
            "branch",
            "export",
            "import",
            "add-dir",
            "stop",
            "bg",
            "exit",
        ),
    ),
    (
        "Model and settings",
        ("tune", "model", "effort", "settings", "config", "output-style", "permissions", "update-config", "focus"),
    ),
    (
        "Autonomy",
        ("goal", "loop", "schedule", "automations", "go", "scope", "proposals", "advisor", "preview", "review"),
    ),
    ("Orchestration", ("ultraplan", "ultracode", "ultraresearch", "maxagents", "artifacts")),
    ("Knowledge", ("memory", "skills", "mcp", "learn", "optimizer", "self-improve")),
    ("System", ("help", "doctor", "update", "stats", "debug", "daemon")),
)


async def _commands_catalog(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """The command list the TUI's /help shows and its slash completion resolves aliases against.

    Without it ``/help`` listed only the TUI's own commands, and `/goal`, `/loop`, `/review`, ... were
    undiscoverable."""
    from k3code import skills as skills_mod
    from k3code.commands._util import session_cwd

    registry = server.commands
    defs = {name: registry.get(name) for name in registry.names()}
    pair = {name: [f"/{name}", (cmd.help.splitlines()[0] if cmd and cmd.help else "")] for name, cmd in defs.items()}
    canon: dict[str, str] = {}
    for name, cmd in defs.items():
        canon[f"/{name}"] = f"/{name}"
        for alias in cmd.aliases if cmd else []:
            canon[f"/{alias}"] = f"/{name}"
    grouped: set[str] = set()
    categories: list[dict[str, Any]] = []
    for title, names in HELP_GROUPS:
        rows = [pair[n] for n in names if n in pair]
        grouped.update(n for n in names if n in pair)
        if rows:
            categories.append({"name": title, "pairs": rows})
    if rest := [pair[n] for n in pair if n not in grouped]:
        categories.append({"name": "Other", "pairs": rest})
    skills = skills_mod.discover(session_cwd(server, params.get("session_id")), list(server.config.skills.roots))
    return {
        "pairs": list(pair.values()),
        "canon": canon,
        "categories": categories,
        "skill_count": len(skills),
        "sub": {},
    }


async def _delegation_status(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """The delegation caps in force for a session (``session_id``, else the current one: the TUI sends none) and what
    is running: ``live.children`` counts that session's sub-agents, ``live.total`` every session's."""
    from k3code.reliability.governor import Governor
    from k3code.subagents.runner import MAX_DEPTH

    live = server._session_for(str(params.get("session_id") or "") or None)
    limits = session_limits(server.config, live) if live is not None else agent_limits(server.config)
    handles = server.subagents.handles.values()
    mine = [h for h in handles if live is not None and h.parent_sid == live.session_id]
    queued = sum(1 for h in mine if h.status == "queued")
    running = sum(1 for h in mine if h.status == "running")
    return {
        "paused": server.subagents.paused,
        "max_spawn_depth": MAX_DEPTH,
        # the width a fan-out really gets (its own ceiling, the session's parallel limit, the governor), not the raw key
        "max_concurrent_children": Governor().agent_cap(limits.fanout_width, ceiling=limits.max_parallel),
        "max_agents": limits.describe_max_agents() if limits.auto else limits.max_agents,
        "dynamic": limits.dynamic,
        "max_parallel": limits.max_parallel,
        "max_nested": limits.max_nested,
        "max_spawns": limits.max_spawns,
        "live": {
            "children": running + queued,
            "queued": queued,
            "total": sum(1 for h in handles if h.status in ("queued", "running")),
        },
    }


async def _delegation_pause(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """`p` in the agents overlay, `/agents pause|resume`: stop or allow new sub-agents (running ones finish)."""
    server.subagents.paused = bool(params.get("paused", not server.subagents.paused))
    return {"paused": server.subagents.paused}


async def _subagent_steer(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """`e` in the agents overlay: a message the running child reads before its next model call."""
    sub_id = str(_require(params, "subagent_id"))
    text = str(_require(params, "text")).strip()
    handle = server.subagents.handles.get(sub_id)
    if handle is None or handle.status != "running":
        return {"status": "not_queued", "message": "that agent is not running"}
    handle.steer_queue.append(text)
    return {"status": "queued"}


_HANDLERS: dict[str, Any] = {
    "session.create": _session_create,
    "session.list": _session_list,
    "session.active_list": _session_active_list,
    "session.resume": _session_resume,
    "session.activate": _session_activate,
    "session.close": _session_close,
    "session.delete": _session_delete,
    "session.title": _session_title,
    "session.interrupt": _session_interrupt,
    "session.steer": _session_steer,
    "session.control.read": _session_control_read,
    "session.control": _session_control,
    "session.workspace.move": _session_workspace_move,
    "session.branch_stored": None,  # filled below (needs create+copy)
    "session.most_recent": _session_most_recent,
    "session.events.since": _session_events_since,
    "session.events.stats": _session_events_stats,
    "session.mode.cycle": _session_mode_cycle,
    "session.mode.set": _session_mode_set,
    "prompt.submit": _prompt_submit,
    "prompt.background": _prompt_background,
    "subagent.list": _subagent_list,
    "subagent.interrupt": _subagent_interrupt,
    "subagent.tail": _subagent_tail,
    "clipboard.paste": _clipboard_paste,
    "paste.collapse": _paste_collapse,
    "process.list": _process_list,
    "image.attach": _image_attach,
    "image.attach_bytes": _image_attach,
    "image.detach": _image_detach,
    "file.attach": _image_attach,
    "pdf.attach": _image_attach,
    "input.detect_drop": _input_detect_drop,
    "command.dispatch": _command_dispatch,
    "slash.exec": _slash_exec,
    "complete.slash": _complete_slash,
    "complete.path": _complete_path,
    "model.options": _model_options,
    "tune.get": _tune_get,
    "settings.get": _settings_get,
    "tune.set": _tune_set,
    "model.save_key": _model_save_key,
    "model.disconnect": _model_disconnect,
    "config.get": _config_get,
    "config.set": _config_set,
    "setup.status": _setup_status,
    "system.battery": _system_battery,
    "spawn_tree.save": _spawn_tree_save,
    "browser.manage": _browser_manage,
    "commands.catalog": _commands_catalog,
    "shell.exec": _shell_exec,
    "shell.cancel": _shell_cancel,
    "session.undo": _session_undo,
    "session.usage": _session_usage,
    "session.status": _session_status,
    "session.save": _session_save,
    "reload.mcp": _reload_mcp,
    "reload.env": _reload_env,
    "skills.reload": _skills_reload,
    "delegation.status": _delegation_status,
    "delegation.pause": _delegation_pause,
    "subagent.steer": _subagent_steer,
}

_HANDLERS["session.branch_stored"] = _session_resume  # M1: fork == resume the source


async def main() -> None:
    """Entry point for ``k3code gateway --stdio``."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = GatewayServer()
    try:
        await server.serve()
    finally:
        await server.close()
