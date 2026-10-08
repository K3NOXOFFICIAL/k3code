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
import json
import logging
import os
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code import confio
from k3code.agent.loop import AgentLoop, ApprovalResult
from k3code.artifacts import ArtifactStore
from k3code.autonomy import advisor, autonomy_cfg
from k3code.autonomy.fanout import FanoutExecutor
from k3code.autonomy.plan_first import GateResult, PlanFirst
from k3code.autonomy.ultra import Ultra
from k3code.blockers import BlockerStore
from k3code.commands import CommandRegistry
from k3code.commands.builtin import build_registry as build_commands
from k3code.config import Settings, load_config
from k3code.errors import AllProvidersUnreachable, ChainExhausted, ContextOverflow
from k3code.extratools import register_mcp_tools, register_skill_tool
from k3code.gateway.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    decode_frame,
    encode_error,
    encode_event,
    encode_response,
    encode_server_request,
    next_request_id,
)
from k3code.gateway.sessions import SessionStore
from k3code.goals import MAX_KICKS_PER_WINDOW, GoalManager, make_judge
from k3code.halt import Halt, clear_halt, load_halt, set_halt
from k3code.learning.hub import LearningHub
from k3code.mcpclient import McpManager
from k3code.paths import project_config_path as _proj_cfg
from k3code.paths import user_config_path as _user_cfg
from k3code.permissions import MODE_CYCLE_NAMES, PermissionMode, permission_mode_from_config, suggest_rules
from k3code.permissions.state import PermissionState, persist_rules, project_config_path
from k3code.prompting import build_system_prompt
from k3code.providers import make_providers
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.redact import redact, scrub_text
from k3code.reliability import BudgetExceeded, DiskGuardFull, Reliability, build_reliability
from k3code.reliability import events as rev
from k3code.reliability.persistent_retry import TurnCancelled
from k3code.research.browser import BrowserManager
from k3code.research.fetch import WebFetcher
from k3code.research.flow import Research
from k3code.research.tools import register_web_tools
from k3code.router import CooldownStore, Router, RouterEvent, build_chain
from k3code.routing.caller import ModelCaller
from k3code.routing.tiers import Escalation, TaskKind, Tier, TierRouters, router_options, tier_for
from k3code.session_ai import compact_messages, make_title
from k3code.subagents import SubagentManager
from k3code.subagents.tools import register_task_tools
from k3code.tools import clip_head_tail
from k3code.usage import UsageDB

logger = logging.getLogger("k3code.gateway")

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


class LiveSession:
    """One active conversation: AgentLoop + router + cached state."""

    def __init__(self, session_id: str, stored: Any, server: GatewayServer) -> None:
        self.session_id = session_id
        self.stored = stored
        self.server = server
        self.system_prompt = _load_system_prompt()  # base prompt; per-turn extras via build_system_prompt
        self.loop: AgentLoop | None = None
        self.turn_task: asyncio.Task[None] | None = None
        self.streaming = False
        #: Serializes whole turns of this session (two prompts, a loop tick and a prompt, ... never interleave two
        #: AgentLoops on one conversation) and unattended runs' set-up/tear-down (see ServerRunner.run_prompt).
        self.turn_lock = asyncio.Lock()
        self.run_lock = asyncio.Lock()
        #: Prompts submitted while a turn was running; each runs as its own turn when the current one ends.
        self.pending_prompts: list[str] = []
        self.last_checkpoint = 0.0  # monotonic time of the last mid-turn persist (see GatewayServer._checkpoint_turn)
        self.idle_since = time.monotonic()  # when the last turn ended (the idle sweeper stops netwatch after a while)
        self.reasoning_effort: str | None = None
        self.todos: list[dict[str, Any]] = []
        self.todo_revision = 0
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
        #: True while a /goal continuation runs: nobody is watching, so bash is sandboxed even in a foreground session.
        self.goal_continuation = False
        self.reliability: Reliability | None = None
        #: paused = waiting on the network/provider; the session still counts as working.
        self.paused = False
        self.paused_since = 0.0
        self.pause_text = ""
        #: Set when the loop guard or a budget stopped the turn; cleared on the next prompt.
        self.needs_input = False
        #: (provider, model) of the latest router attempt, for usage rows.
        self.last_entry: tuple[str, str] = ("", "")
        #: M4a: tier and task kind of the call in flight (for usage rows); /scope override for the next task.
        self.last_tier = "main"
        self.current_kind = "interactive_turn"
        self.scope_override: str | None = None
        #: /advisor text awaiting "accept" (kept out of the main context until then).
        self.pending_advisor: str = ""
        #: Tool installers run on each turn's registry (loop sessions add ``schedule_next``).
        self.extra_tools: list[Callable[[Any], None]] = []
        #: Outcome of the last unattended run (``completed`` / ``failed``); cleared when a new turn starts.
        self.run_result: str | None = None
        #: What the last turn ended with, for the cron/loop runners.
        self.last_error = ""
        self.last_exc: BaseException | None = None
        self.last_api_calls = 0
        #: Task kind for the next unattended turn (``loop_tick`` / ``cron_job`` / ``background_turn``).
        self.task_kind: str = ""
        #: /go after /ultraplan: {task, plan, path}; consumed by the next turn's scope gate.
        self.preapproved_plan: dict[str, Any] | None = None

    @property
    def state(self) -> str:
        """``working`` (also while paused), ``needs_input``, ``completed``/``failed`` (unattended run) or ``idle``."""
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
        self.server.store.save(self.stored)
        self.emit("session.info", self.live_info())

    @property
    def history(self) -> list[Message]:
        """Stored messages as provider Messages, system entry excluded."""
        out: list[Message] = []
        for m in self.stored.messages:
            role = m.get("role")
            if role == "system" or not role:
                continue
            out.append(
                Message(
                    role=role,
                    content=m.get("content"),
                    tool_call_id=m.get("tool_call_id"),
                    name=m.get("name"),
                    # Without these the next turn sent `tool` results with no assistant tool_calls: HTTP 400 on
                    # OpenAI-compatible and Anthropic providers, for every session that had used a tool.
                    tool_calls=[
                        ToolCall(id=str(tc.get("id") or ""), name=str(tc.get("name") or ""),
                                 arguments=dict(tc.get("arguments") or {}),
                                 raw_arguments=tc.get("raw_arguments"))
                        for tc in (m.get("tool_calls") or [])
                    ],
                )
            )
        return out

    def live_info(self) -> dict[str, Any]:
        """SessionLiveInfo payload for session.create/resume/activate results."""
        config = self.server.config
        return {
            "model": self.stored.model or config.default_model,
            "provider": self.stored.provider,
            "reasoning_effort": self.reasoning_effort,
            "approval_mode": self.perms.mode.value,
            "mode": self.perms.mode.value,
            "yolo": self.perms.mode == PermissionMode.YOLO,
            "add_dirs": list(self.perms.add_dirs),
            "cwd": self.stored.cwd,
            "title": self.stored.title,
            "stored_session_id": self.session_id,
            "running": self.streaming,
            "state": self.state,
            "paused": self.paused,
            "background": self.background,
        }


#: Conversation size at which a session's older messages are folded into a summary, and how many recent ones stay.
CONTEXT_DEFAULTS: dict[str, Any] = {"compact_at_tokens": 80_000, "keep_messages": 8}


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough size of what a conversation sends (~4 characters per token), tool calls included.

    Tool results are counted as the model receives them (head+tail clip), so the stored full text does not trigger
    compaction early; everything else is counted as stored.
    """
    chars = 0
    for m in messages:
        c = m.get("content")
        if m.get("role") == "tool" and isinstance(c, str):
            c = clip_head_tail(c)
        chars += len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False)) if c else 0
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"], ensure_ascii=False))
    return chars // 4


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

    def __init__(self, send: Callable[[str], None], name: str = "stdio") -> None:
        self.send = send
        self.name = name
        self.session_id: str | None = None
        self.closed = False
        self.pending_bytes = 0  # queued for the peer but not yet accepted by its socket (see MAX_CLIENT_BACKLOG)
        self.close_peer: Callable[[], None] | None = None  # drops the connection (socket clients)


#: The client whose request is being handled (so replies and "current session" resolve per client).
_ctx_client: contextvars.ContextVar[Client | None] = contextvars.ContextVar("k3_client", default=None)
#: The session a turn task is running, so router/reliability events reach its clients.
_ctx_session: contextvars.ContextVar[LiveSession | None] = contextvars.ContextVar("k3_session", default=None)


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
        self.config = config or load_config(project_dir=Path.cwd())
        self.store = store or SessionStore(self._home() / "sessions.db")
        self._stdin = stdin
        self._stdout = stdout
        self.commands: CommandRegistry = build_commands()
        self.live: dict[str, LiveSession] = {}
        self.mcp = McpManager(self.config.mcp.servers)
        # one pooled client, cache and rate budget per gateway, shared by every session's web tools
        self.web_fetcher = WebFetcher.from_config(self.config.research)
        self.browser = BrowserManager.from_config(self.config)  # launched on first use, never at start-up
        self.goal_judge: Any = None  # test hook: async (goal, last_text, session) -> (verdict, reason)
        self.providers: list[Any] = []
        self.router: Router | None = None
        self.cooldowns = CooldownStore(path=self._home() / "cooldowns.json")
        self._running = False
        self._server_request_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        #: Server→client requests still unanswered: id → (session_id, frame). Re-sent on attach.
        self._open_requests: dict[str, tuple[str, str]] = {}
        self._stdio_client = Client(lambda line: self._write(line))
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
        self.artifacts = ArtifactStore(self._home() / "artifacts.db")
        self._tiers: TierRouters | None = None
        self._router_cache: dict[str, TierRouters] = {}  # one TierRouters per model key (see _ensure_router)
        self._side_tasks: set[asyncio.Task[Any]] = set()  # fire-and-forget work (titles)
        self._idle_sweeper: asyncio.Task[None] | None = None
        #: (provider, model) of the most recent router attempt on any task (side calls read it).
        self.last_attempt: tuple[str, str] = ("", "")
        self.model_caller = ModelCaller(
            self.tier_routers,
            self.config,
            self.usage,
            emit=lambda t, p: self.emit(t, p),
            last_attempt=lambda: self.last_attempt,
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
            reader = asyncio.StreamReader()
            await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

        self._send_ready(self._stdio_client)

        while self._running:
            line = await reader.readline()
            if not line:
                logger.info("stdin EOF; gateway stdio detached")
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            if self._is_concurrent_request(text):  # may wait for this very pipe's answer (clarify): don't block reading
                task = asyncio.get_running_loop().create_task(self._guarded_handle(text, None))
                self._side_tasks.add(task)
                task.add_done_callback(self._side_tasks.discard)
                continue
            await self._guarded_handle(text, None)

    async def start_socket(self, path: Path | str) -> None:
        """Listen on a Unix socket: one JSON-RPC connection per client, sessions shared."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _socket_is_live(path):
                raise RuntimeError(f"{path} is served by another process; not taking it over")
            path.unlink()  # stale socket from a crashed daemon
        self._socket_server = await asyncio.start_unix_server(self._on_connect, path=str(path))
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

    socket_path: Path

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
    #: keeps reading and can deliver that answer. Awaited inline they deadlocked their own connection.
    CONCURRENT_METHODS = frozenset({"command.dispatch", "slash.exec"})

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
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if self._is_concurrent_request(text):
                    task = asyncio.get_running_loop().create_task(self._guarded_handle(text, client))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    continue
                await self._guarded_handle(text, client)
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

    def _is_concurrent_request(self, text: str) -> bool:
        try:
            frame = json.loads(text)
        except ValueError:
            return False
        return isinstance(frame, dict) and frame.get("method") in self.CONCURRENT_METHODS

    async def _guarded_handle(self, text: str, client: Client | None) -> None:
        try:
            await self._handle_line(text, client)
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
                    "started_at": s.stored.created_at,
                    "status": s.state,
                    "state": s.state,
                    "paused": s.paused,
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
                    "last_active": h.started_at,
                    "message_count": h.tool_count,
                    "model": h.model or h.tier,
                    "preview": h.description[:120],
                    "session_key": h.id,
                    "started_at": h.started_at,
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
            with contextlib.suppress(Exception):
                await self.sweep_idle_reliability()

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
            if rel is None or not rel._started or live.streaming or live.pending_approval is not None:
                continue
            if now - live.idle_since < self.IDLE_RELIABILITY_S:
                continue
            with contextlib.suppress(Exception):
                await rel.stop()  # re-armed by _reliability_for on the session's next turn
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
        if self.automation is not None:
            await self.automation.stop()
        await self.web_fetcher.aclose()
        await self.browser.close()
        cancelled = []
        for live in self.live.values():
            if live.turn_task is not None and not live.turn_task.done():
                live.turn_task.cancel()
                cancelled.append(live.turn_task)
        if cancelled:  # let each turn persist what it has (its finally block) before the store is closed below
            await asyncio.wait(cancelled, timeout=5.0)
        for live in self.live.values():
            if live.reliability is not None:
                with contextlib.suppress(Exception):
                    await live.reliability.stop()
        await self.mcp.close()
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

    async def _handle_line(self, text: str, client: Client | None = None) -> None:
        client = client or self._stdio_client
        self._loop = asyncio.get_running_loop()
        if self.panes is not None and client is self._stdio_client:
            self.panes.on_client_line(text)
        token = _ctx_client.set(client)
        try:
            await self._dispatch_line(text, client)
        finally:
            _ctx_client.reset(token)

    async def _dispatch_line(self, text: str, client: Client) -> None:
        obj, err = decode_frame(text)
        if err is not None:
            kind = PARSE_ERROR if err.startswith("parse error") else INVALID_REQUEST
            self._reply(client, encode_error(None, kind, err))
            return
        assert obj is not None
        req_id = obj.get("id")
        method = obj.get("method")
        params = obj.get("params") or {}

        if method is None:
            # A result frame answering one of our server→client requests.
            self._resolve_server_request(req_id, obj)
            return

        if req_id is None:
            # Notification: nothing to answer. M1 has no notification methods.
            logger.debug("ignoring notification %s", method)
            return

        handler = _HANDLERS.get(method)
        if handler is None:
            self._reply(client, encode_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}"))
            return

        try:
            result = await handler(self, params)
        except _InvalidParams as e:
            self._reply(client, encode_error(req_id, INVALID_PARAMS, str(e)))
        except Exception as e:  # noqa: BLE001 - one bad method must not kill the gateway
            logger.exception("method %s failed", method)
            self._reply(client, encode_error(req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}"))
        else:
            self._reply(client, encode_response(req_id, result))

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
            self.notify_blocker(session_id, f"Waiting for an answer: {params.get('command') or method}",
                                level="info", kind="approval", key=f"k3.approval.{req_id}")
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
        dir per claude-cli provider, none of the old ones closed. Routers are now cached per key; the providers
        and the cooldown store are rebuilt only when the provider config itself is replaced (/config reload).
        """
        key = model or self.config.default_model
        # _router_cfg None: a router injected from outside (tests) is taken as is
        current = self._router_cfg is None or self._router_cfg is self.config.providers
        if self.router is not None and self._chain_key == key and current:
            return
        if self._router_cfg is not self.config.providers:  # first build, or the provider config was replaced
            self._retire_providers(self.providers)
            self.providers = make_providers(self.config.providers)
            self.cooldowns = CooldownStore(path=self._home() / "cooldowns.json")
            self._router_cache = {}
            self.__dict__.pop("_oneshot_routers", None)  # one-shot routers hold chains of the replaced providers
            self._router_cfg = self.config.providers
        tiers = self._router_cache.get(key)
        if tiers is None:
            tiers = self._router_cache[key] = TierRouters(
                self.providers, self.config, cooldowns=self.cooldowns, on_event=self._on_router_event, main_key=key
            )
        self._tiers = tiers
        self.router = tiers.get(Tier.MAIN)
        self._chain_key = key

    def _retire_providers(self, providers: list[Any]) -> None:
        """Close replaced providers in the background (their httpx pools / temp dirs would otherwise wait for GC)."""
        for p in providers:
            try:
                task = asyncio.get_running_loop().create_task(p.aclose())
            except RuntimeError:  # no running loop (sync setup in tests): nothing to await it on
                continue
            self._side_tasks.add(task)
            task.add_done_callback(self._side_tasks.discard)

    _chain_key: str | None = None
    _router_cfg: Any = None  # the config.providers object the cached providers/routers were built from

    def tier_routers(self) -> TierRouters:
        """The per-tier routers (built with the main router; rebuilt when the model key changes)."""
        if self._tiers is None:
            if self.router is not None:  # a router injected from outside (tests): every tier shares it
                self._tiers = TierRouters([], self.config, cooldowns=self.cooldowns, fallback_router=self.router)
            else:
                self._ensure_router()
        assert self._tiers is not None
        return self._tiers

    async def _reliability_for(self, session: LiveSession) -> Reliability:
        """One Reliability bundle per session: netwatch runs while the session lives, events go to its clients."""
        if session.reliability is None:
            rel = build_reliability(self.config, session=session.session_id, home=self._home())
            rel.events.add(lambda e: self._on_reliability_event(session, e), key="gateway")
            session.reliability = rel
        assert self.router is not None
        session.reliability.register_providers(self.router.chain)
        with contextlib.suppress(Exception):
            await session.reliability.start()
        return session.reliability

    def _on_router_event(self, event: RouterEvent) -> None:
        """Router events: failover/retry/exhausted → TUI + usage rows. Reliability kinds go via the session sink."""
        sess = _ctx_session.get()
        sid = sess.session_id if sess else ""
        if event.kind == "router.attempt":
            self.last_attempt = (event.provider, event.model)
            if sess is not None:
                sess.last_entry = (event.provider, event.model)
                sess.last_tier = str(event.extra.get("tier", "main"))
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
            self.emit("error", {"message": f"All providers failed: {event.detail}"})

    #: Notification key shared by pause/park toasts so ``resumed`` can clear them.
    PAUSE_KEY = "k3.reliability.pause"

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
            label = "Budget exceeded" if kind == rev.BUDGET_EXCEEDED else "Loop detected"
            session.emit("error", {"message": f"{label}: {detail}"}, importance=ess)
        elif kind == rev.NEEDS_INPUT:
            session.needs_input = True

    # ── turn lifecycle ────────────────────────────────────────────────

    async def _run_turn(self, session: LiveSession, text: str) -> tuple[str, str]:
        """One user prompt, then (while a /goal is active) judge + auto-continue until done/paused/budget."""
        async with session.turn_lock:  # a second turn on this session waits instead of interleaving with the first
            try:
                result = await self._run_turn_locked(session, text)
                # A prompt typed mid-turn used to be answered "queued" and then dropped. Run those now, in order.
                # halted: keep the queued prompts for after /daemon resume
                while session.pending_prompts and result[0] not in ("interrupted", "halted"):
                    result = await self._run_turn_locked(session, session.pending_prompts.pop(0))
                return result
            finally:
                session.goal_continuation = False

    async def _run_turn_locked(self, session: LiveSession, text: str) -> tuple[str, str]:
        _ctx_session.set(session)  # this task's events belong to the session, not to the requesting client
        prompt = text
        mgr = self.goal_manager(session)
        session.goal_continuation = False  # the prompt the user sent is not a continuation
        while True:
            try:
                await self._maybe_compact(session)
                n_before = len(session.stored.messages)
                status, final_text = await self._run_one_turn(session, prompt)
                if status == "error" and isinstance(session.last_exc, ContextOverflow):
                    # The provider says the conversation does not fit: drop this attempt's messages, fold the older
                    # history into a summary, and run the prompt once more.
                    session.stored.messages = session.stored.messages[:n_before]
                    if await self._maybe_compact(session, force=True):
                        status, final_text = await self._run_one_turn(session, prompt)
            except asyncio.CancelledError:
                self._block_goal_for(session, "interrupted")
                raise
            if self.halted and mgr.is_active() and status == "done":
                status = "halted"  # the halt arrived while the turn ran: no judge call, no continuation
            if status != "done" or not mgr.is_active():
                self._block_goal_for(session, status)  # an active goal never ends silently
                self._session_finished(session, status)
                return status, final_text
            judge = self.goal_judge or make_judge(self._goal_completer(session))
            decision = await mgr.evaluate_after_turn(
                final_text, judge, cwd=session.stored.cwd or None, reviewer=self._goal_reviewer(session)
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

    def _block_goal_for(self, session: LiveSession, status: str) -> None:
        """A turn that did not finish leaves its active goal paused, with the reason and one notification.

        ``needs_input`` (budget, loop guard, a question), ``interrupted`` (/stop, a graceful stop), ``halted``
        (/daemon pause) and ``error`` each get their own pause reason, so nothing waits in the background silently.
        """
        mgr = self.goal_manager(session)
        if not mgr.is_active():
            return
        if status == "needs_input":
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
        self.notify_blocker(session.session_id, text, level="warning", kind="goal",
                            key=f"k3.goal.blocked.{session.session_id}")

    def _session_finished(self, session: LiveSession, status: str) -> None:
        """Tell the automation engine (``session_event`` triggers) that a session's run ended."""
        if self.automation is not None:
            self.automation.session_event(session.session_id, status, str(session.stored.meta.get("origin") or ""))

    def _build_loop(
        self,
        session: LiveSession,
        reliability: Reliability,
        router: Router,
        kind: TaskKind,
        approval: Any,
        *,
        max_tool_errors: int = 0,
    ) -> AgentLoop:
        loop = AgentLoop(
            router,
            system_prompt=build_system_prompt(
                session.system_prompt,
                cwd=session.perms.cwd,
                config=self.config,
                session_meta=session.stored.meta,
                mcp=self.mcp,
            ),
            max_turns=self.config.max_turns,
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
        )
        loop.on_checkpoint = lambda: self._checkpoint_turn(session)  # prompt + tool call hit the disk before the tool
        register_skill_tool(loop.tools, session.perms.cwd, list(self.config.skills.roots))
        register_mcp_tools(loop.tools, self.mcp)
        for install in session.extra_tools:
            install(loop.tools)
        register_task_tools(loop.tools, self, session, depth=1)
        register_web_tools(loop.tools, self.config, fetcher=self.web_fetcher, mcp=self.mcp, browser=self.browser)
        return loop

    async def _run_one_turn(self, session: LiveSession, text: str) -> tuple[str, str]:
        """Execute one prompt end-to-end, emitting wire events. Returns (status, final_text)."""
        if self.halted:  # /daemon pause: nothing reaches a provider; the caller pauses the goal (status 'halted')
            return "halted", ""
        self._ensure_router(session.stored.model or None)
        assert self.router is not None
        session.perms.cwd = Path(session.stored.cwd or Path.cwd())  # session cwd, never the process cwd
        session.perms.reload()
        session.needs_input = False
        session.run_result = None
        session.last_error, session.last_exc, session.last_api_calls = "", None, 0
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
        max_errors = int(autonomy_cfg(self.config)["escalate"]["tool_errors"]) if cheap_start else 0
        escalation = Escalation(tier, thresholds={"tool_errors": 1, "loop_guard": 1})  # the loop counted already

        config = self.config
        final_text = ""
        usage = Usage()
        error: str | None = None
        status = "done"
        gate = GateResult(prompt=text)
        loop = self._build_loop(
            session, reliability, self.tier_routers().get(tier), kind, approval, max_tool_errors=max_errors
        )
        session.loop = loop

        async def on_text_delta(chunk: str) -> None:
            nonlocal final_text
            final_text += chunk
            session.emit("message.delta", {"text": chunk})

        async def on_text_reset() -> None:
            nonlocal final_text
            final_text = ""  # a retried attempt streams the answer again; judge and loop hash want one copy

        loop.on_text_delta = on_text_delta
        loop.on_text_reset = on_text_reset

        session.emit("message.start", {})
        session.emit("status.update", {"kind": "status", "text": "thinking", "state": "working"})
        session.streaming = True
        session.current_kind = kind.value
        try:
            try:
                gate = await self.autonomy.prepare(session, text)  # M4a: scope gate + planning turn
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
            ):
                # A trivial task is "unimportant work": start it on the cheap tier. The loop escalates to main when the
                # attempt stalls (tool errors, loop guard), so a task the cheap model cannot do still gets done.
                tier, cheap_start = Tier.CHEAP, True
                max_errors = int(acfg["escalate"]["tool_errors"])
                escalation = Escalation(tier, thresholds={"tool_errors": 1, "loop_guard": 1})
                loop = self._build_loop(
                    session, reliability, self.tier_routers().get(tier), kind, approval, max_tool_errors=max_errors
                )
                loop.on_text_delta = on_text_delta
                loop.on_text_reset = on_text_reset
                loop.on_checkpoint = lambda: self._checkpoint_turn(session)
                session.loop = loop
            history = session.history
            prompt = gate.prompt
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
                new_tier = escalation.record(loop.escalation_reason) if cheap_start and loop.escalation_reason else None
                if new_tier is None or loop.interrupted:
                    break
                # The attempt stalled on a cheap tier: continue the same task one tier up.
                reason = loop.escalation_reason or "unknown"
                if "loop_guard" in reason:
                    self.usage.record("loop_guard", session=session.session_id, detail=reason)
                self.model_caller.note_escalation(kind, tier, new_tier, reason, session.session_id)
                tier = new_tier
                session.needs_input = False
                if reliability.loop_guard is not None:
                    reliability.loop_guard.reset()
                history = [m for m in loop.turn_messages if m.role != "system"]
                prompt = (
                    f"The previous attempt stalled ({reason}). Continue the task from where it left off, "
                    "with a different approach if needed."
                )
                loop = self._build_loop(
                    session, reliability, self.tier_routers().get(tier), kind, approval, max_tool_errors=max_errors
                )
                loop.on_text_delta = on_text_delta
                loop.on_text_reset = on_text_reset
                session.loop = loop
        except (AllProvidersUnreachable, ChainExhausted, ContextOverflow, DiskGuardFull) as e:
            status = "error"
            error = str(e)
            session.last_exc = e
            session.emit("error", {"message": str(e)})
        except BudgetExceeded as e:
            # The reliability event already told the client (error + needs_input).
            status = "needs_input"
            error = str(e)
            session.needs_input = True
        except TurnCancelled:
            status = "interrupted"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("turn failed")
            status = "error"
            error = str(e)
            session.last_exc = e
            session.emit("error", {"message": str(e)})
        finally:
            session.streaming = False
            session.idle_since = time.monotonic()
            # Persist whatever the loop accumulated, also on error and on /stop or shutdown (CancelledError):
            # this used to sit after the try block, which a cancellation skipped, so the whole turn vanished.
            self._persist_turn(session, loop)
            if session.paused:  # a cancelled wait never saw "resumed"
                session.paused = False
                session.emit("notification.clear", {"key": self.PAUSE_KEY}, importance="essential")
            self.autonomy.finish(session, gate, status, final_text, text)
            if self.learning.enabled:
                self.learning.spawn(self.learning.turn_finished(session, status))

        session.last_error = error or ""
        session.last_api_calls = sum(1 for m in loop.turn_messages if m.role == "assistant")
        if session.background:
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
                "usage": _usage_payload(usage),
                "status": status,
                "error": error,
                "state": session.state,
            },
        )
        session.emit("status.update", {"kind": "status", "text": "", "state": session.state})
        if status == "done" and not session.stored.title and autonomy_cfg(self.config)["auto_title"]:
            task = asyncio.create_task(self._auto_title(session, text))
            self._side_tasks.add(task)
            task.add_done_callback(self._side_tasks.discard)
        return status, final_text

    #: Minimum seconds between mid-turn checkpoints (each one rewrites the session's message list).
    CHECKPOINT_EVERY_S = 3.0

    def _persist_turn(self, session: LiveSession, loop: AgentLoop) -> None:
        """Write the loop's conversation so far to the session store. Never raises: it runs in ``finally``."""
        try:
            if loop.turn_messages:
                session.stored.messages = _serialize_messages(loop.turn_messages)
                session.stored.model = session.stored.model or self._chain_key or self.config.default_model
                self.store.save(session.stored)
            session.last_checkpoint = time.monotonic()
        except Exception:  # noqa: BLE001 - a failed checkpoint must not mask the turn's own outcome
            logger.exception("could not persist the session")

    def _checkpoint_turn(self, session: LiveSession) -> None:
        """After a tool result: persist the in-flight turn (throttled) so kill -9, an OOM kill or a reboot loses
        seconds of work instead of the whole turn."""
        loop = session.loop
        if loop is None or time.monotonic() - session.last_checkpoint < self.CHECKPOINT_EVERY_S:
            return
        self._persist_turn(session, loop)

    async def _maybe_compact(self, session: LiveSession, *, force: bool = False) -> int:
        """Fold the older part of the conversation into a summary once it is large; returns how many messages folded.

        Nothing compacted automatically, so a `/loop` living in one session grew its history forever: every tick
        re-sent and re-stored all of it, and on a real model the context window was exceeded after a few hundred ticks
        and every later tick failed with ContextOverflow. Runs on the cheap ``compaction`` tier; failures are logged and
        the turn goes on.
        """
        if self.halted:  # the compaction call is a model call too
            return 0
        cfg = {**CONTEXT_DEFAULTS, **dict(getattr(self.config, "context", None) or {})}
        messages = session.stored.messages
        if not force and _estimate_tokens(messages) < int(cfg["compact_at_tokens"]):
            return 0
        try:
            new, folded = await compact_messages(
                self.model_caller, list(messages), keep=int(cfg["keep_messages"]), session_id=session.session_id
            )
        except Exception:  # noqa: BLE001 - e.g. every provider rate-limited: the turn proceeds with the long history
            logger.warning("automatic compaction failed", exc_info=True)
            return 0
        if folded:
            session.stored.messages = new
            self.store.save(session.stored)
            logger.info("compacted %d messages of session %s (%d left)", folded, session.session_id, len(new))
            session.emit("notification.show", {"text": f"Context compacted: {folded} older messages summarized",
                                               "level": "info", "kind": "info", "key": "k3.compact"})
        return folded

    async def _auto_title(self, session: LiveSession, first_message: str) -> None:
        """Name a fresh session on the ``title`` task kind; best-effort, never surfaces errors."""
        title = await make_title(self.model_caller, first_message, session_id=session.session_id)
        if title and not session.stored.title:
            session.stored.title = title
            self.store.save(session.stored)
            session.emit("session.title", {"session_id": session.session_id, "title": title})

    def _on_stream_event(self, session: LiveSession, event: StreamEvent) -> None:
        if event.type == "tool_call" and event.tool_call:
            session.emit("tool.generating", {"name": event.tool_call.name})
            session.emit(
                "tool.start",
                {
                    "tool_id": event.tool_call.id,
                    "name": event.tool_call.name,
                    "args": event.tool_call.arguments,
                },
            )
            self.usage.record("tool", session=session.session_id, detail=event.tool_call.name)
        elif event.type == "done" and event.message:
            msg = event.message
            if msg.role == "tool":
                self._checkpoint_turn(session)
                session.emit(
                    "tool.complete",
                    {
                        "tool_id": msg.tool_call_id or "",
                        "name": msg.name or "",
                        "result_text": msg.content or "",
                        "result": {"content": msg.content},
                    },
                )
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
                    tier=session.last_tier,
                    task_kind=session.current_kind,
                )
                if u:
                    session.emit("session.usage", {"usage": _usage_payload(u)})

    async def _approval_callback_for(self, session: LiveSession) -> Any:
        async def approve(tool_name: str, arguments: dict[str, Any], decision: Any = None) -> ApprovalResult:
            decision = decision or session.perms.decide(tool_name, arguments)
            rules = suggest_rules(tool_name, decision)
            pattern = ", ".join(r.pattern for r in rules)
            self.usage.record("approval", session=session.session_id, detail=tool_name)
            try:
                result = await self._ask_client(
                    "approval",
                    {
                        "request_id": next_request_id("approval"),
                        "command": _command_for_tool(tool_name, arguments),
                        "description": decision.message
                        or arguments.get("description")
                        or arguments.get("path")
                        or arguments.get("pattern")
                        or "",
                        "choices": ["once", "session", "always", "deny"],
                        "allow_permanent": True,
                        "allow_session": True,
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
            reason = str(result.get("reason") or result.get("text") or "").strip()
            if choice in ("session", "always"):
                session.perms.session_rules.extend(rules)
            if choice == "always" and rules:
                persist_rules(project_config_path(session.perms.cwd), rules)
            self.learning.approval(session, tool_name, pattern, choice)
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

    def apply_file_config(self, cwd: str | Path | None = None) -> None:
        """Re-read config files into the live settings (only keys present in a file are replaced)."""
        base = Path(cwd) if cwd else Path.cwd()
        fresh = load_config(project_dir=base)
        keys: set[str] = set()
        for path in (_user_cfg(), _proj_cfg(base)):
            try:
                keys |= set(confio.read_yaml(path))
            except confio.ConfigError:
                continue
        for key in keys & set(Settings.model_fields):
            setattr(self.config, key, getattr(fresh, key))
        if "providers" in keys:
            self.router = None
            self._tiers = None
        self.mcp.configure(self.config.mcp.servers)

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

        With a task ``kind`` (and no explicit ``model_key``) the call goes through :class:`ModelCaller`
        (tier policy, escalation, tier/kind usage rows); otherwise through a plain chain for ``model_key``.
        """
        messages = [Message(role="system", content=system), Message(role="user", content=user)]
        if kind is not None and model_key is None:
            res = await self.model_caller.complete(kind, messages, session_id=session_id, max_tokens=max_tokens)
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
        """M4a: a strong-tier advisor veto on ``done`` (``autonomy.advisor_on_goal``); None when disabled."""
        cfg = autonomy_cfg(self.config)
        if not cfg.get("advisor_on_goal"):
            return None

        async def review(goal: str) -> tuple[bool, list[str]]:
            ctx = await advisor.condensed_context(
                self.model_caller,
                session.stored.messages,
                threshold=int(cfg["advisor_compact_chars"]),
                session_id=session.session_id,
            )
            return await advisor.review_done(self.model_caller, goal, ctx, session_id=session.session_id)

        return review

    def goal_manager(self, session: LiveSession) -> GoalManager:
        def load() -> dict[str, Any] | None:
            return session.stored.meta.get("goal")

        def save(state: dict[str, Any] | None) -> None:
            if state is None:
                session.stored.meta.pop("goal", None)
            else:
                session.stored.meta["goal"] = state
            self.store.save(session.stored)

        return GoalManager(load, save, default_max_turns=self.config.goal.max_turns)

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
        if live.streaming or (live.turn_task is not None and not live.turn_task.done()):
            return False  # a live turn is working on it already
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

    def notify_blocker(self, session_id: str, text: str, *, level: str = "warning", kind: str = "goal",
                       key: str = "") -> int:
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
        if session.streaming or (session.turn_task is not None and not session.turn_task.done()):
            raise _InvalidParams("a turn is already running in this session; /stop it or wait")

        async def runner() -> None:
            _ctx_session.set(session)
            session.needs_input = False
            session.streaming = True
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
                self.subagents.interrupt_session(session.session_id)  # nothing may outlive the job
            session.emit("message.delta", {"text": text})
            session.stored.messages = [
                *session.stored.messages,
                {"role": "user", "content": label},
                {"role": "assistant", "content": text},
            ]
            self.store.save(session.stored)
            session.emit(
                "message.complete", {"text": text, "usage": {}, "status": status, "error": None, "state": session.state}
            )
            session.emit("status.update", {"kind": "status", "text": "", "state": session.state})

        session.turn_task = asyncio.get_running_loop().create_task(runner())

    # ── background sessions (/bg, Ctrl+B) ─────────────────────────────

    def _fresh_session_like(self, src: LiveSession, *, background: bool = False) -> LiveSession:
        """A new session with ``src``'s cwd, model, permission mode and add-dirs."""
        stored = self.store.create(
            model=src.stored.model or self.config.default_model,
            provider=src.stored.provider or "",
            cwd=src.stored.cwd or str(Path.cwd()),
        )
        stored.meta["mode"] = src.perms.mode.value
        stored.meta["add_dirs"] = list(src.perms.add_dirs)
        if background:
            stored.meta["background"] = True
            stored.meta["origin_session"] = src.session_id
        self.store.save(stored)
        live = LiveSession(stored.session_id, stored, self)
        live.reasoning_effort = src.reasoning_effort
        self.live[live.session_id] = live
        return live

    def start_background(self, origin: LiveSession, prompt: str) -> LiveSession:
        """``/bg <prompt>``: run ``prompt`` in a new background session; notify ``origin`` when it ends."""
        if self.halted:
            raise _InvalidParams("daemon is halted (/daemon pause); resume with /daemon resume")
        if self.background_paused:
            raise _InvalidParams("background work is paused (restart-storm safe mode); resume with /daemon resume")
        live = self._fresh_session_like(origin, background=True)
        live.stored.title = live.stored.title or " ".join(prompt.split())[:60]
        self.store.save(live.stored)
        live.turn_task = asyncio.get_running_loop().create_task(self._run_turn(live, prompt))
        self._watch_background(live, origin.session_id)
        return live

    def background_current(self, session: LiveSession, client: Client | None) -> LiveSession:
        """Ctrl+B: the running turn keeps going as a background session; the client gets a fresh foreground one."""
        session.background = True
        session.stored.meta["background"] = True
        session.stored.meta["origin_session"] = session.session_id
        if session.loop is not None:
            session.loop.background = True  # bash is sandboxed from now on
        self.store.save(session.stored)
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


def _stored_tool_call(tc: ToolCall) -> dict[str, Any]:
    """A tool call as stored. ``raw_arguments`` (the model's own argument text) is kept: a turn rebuilt from storage
    must re-send the same bytes as the turn that made the call, or the provider's prompt cache misses from there."""
    stored: dict[str, Any] = {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
    if tc.raw_arguments is not None:
        stored["raw_arguments"] = tc.raw_arguments
    return stored


def _command_for_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """Human-readable command line shown in the approval prompt."""
    if tool_name == "bash":
        return str(arguments.get("command", ""))
    if tool_name in ("write", "edit"):
        return f"{tool_name} {arguments.get('path', '')}"
    if tool_name == "read":
        return f"read {arguments.get('path', '')}"
    return f"{tool_name} {json.dumps(arguments, ensure_ascii=False)[:120]}"


def _usage_payload(usage: Usage) -> dict[str, Any]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.prompt_tokens + usage.completion_tokens,
    }


# ── M1-core method handlers ──────────────────────────────────────────────


def _require(params: dict[str, Any], key: str) -> Any:
    value = params.get(key)
    if value is None or value == "":
        raise _InvalidParams(f"missing required param: {key}")
    return value


async def _session_create(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    stored = server.store.create(
        model=params.get("model") or server.config.default_model,
        provider=params.get("provider") or "",
        cwd=params.get("cwd") or str(Path.cwd()),
    )
    if params.get("background"):
        stored.meta["background"] = True
        server.store.save(stored)
    live = LiveSession(stored.session_id, stored, server)
    live.reasoning_effort = params.get("effort")
    server.session = live
    if server.learning.enabled and not params.get("background"):
        server.learning.spawn(server.learning.prepare_project(live))
    return {"session_id": stored.session_id, "info": live.live_info()}


async def _session_list(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    limit = int(params.get("limit") or 50)
    rows = []
    for s in server.store.list(limit=limit):
        preview = ""
        for m in s.messages:
            if m.get("role") == "user" and m.get("content"):
                preview = str(m["content"])[:120]
                break
        live = server.live.get(s.session_id)
        state = live.state if live is not None else ("completed" if s.messages else "new")
        rows.append(
            {
                "id": s.session_id,
                "title": s.title or preview or "Session",
                "preview": preview,
                "started_at": s.created_at,
                "message_count": len(s.messages),
                "status": state,
                "state": state,
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


def transcript_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stored (OpenAI-shaped) messages -> the ``{role, text, name, context}`` rows the TUI renders on resume.

    System prompts and assistant tool-call stubs are dropped; a tool result becomes a ``tool`` row named after
    the call that produced it.
    """
    names: dict[str, tuple[str, str]] = {}
    rows: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                names[str(tc.get("id"))] = (str(tc.get("name") or fn.get("name") or "tool"),
                                            str(tc.get("arguments") or fn.get("arguments") or "")[:80])
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        if role == "tool":
            name, ctx = names.get(str(m.get("tool_call_id")), (str(m.get("name") or "tool"), ""))
            rows.append({"role": "tool", "name": name, "context": ctx})
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
    return {"session_id": stored.session_id, "info": live.live_info(), "status": live.state,
            "running": live.streaming, "messages": transcript_rows(live.stored.messages)}


async def _session_close(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """The TUI closes the session it just left (``/resume``, new session). An idle foreground session is dropped from
    the live registry (its stored copy stays resumable); one that is running, backgrounded or waiting for an
    answer keeps going, so it stays in the agent strip."""
    sid = str(params.get("session_id") or "")
    live = server.live.get(sid)
    if live is None:
        return {"closed": False, "reason": "not live"}
    if (live.streaming or live.background or live.needs_input or server.has_open_request(sid)
            or any(c.session_id == sid for c in server.clients)):
        return {"closed": False, "reason": "still in use"}
    server.live.pop(sid, None)
    if live.reliability is not None:
        await live.reliability.stop()
    return {"closed": True}


async def _session_delete(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    deleted = server.store.delete(str(sid))
    live = server.live.pop(str(sid), None)
    if live is not None:
        if live.turn_task is not None and not live.turn_task.done():
            live.turn_task.cancel()
        if live.reliability is not None:
            await live.reliability.stop()
    for client in server.clients:
        if client.session_id == sid:
            client.session_id = None
    return {"deleted": deleted}


async def _session_title(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    title = _require(params, "title")
    stored = server.store.get(str(sid))
    if stored is None:
        raise _InvalidParams(f"unknown session: {sid}")
    stored.title = str(title)
    server.store.save(stored)
    return {"ok": True}


async def _session_interrupt(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    interrupted = await server.interrupt_turn(params.get("session_id"))
    return {"interrupted": interrupted}


async def _session_steer(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    text = _require(params, "text")
    session = server.session
    if session is None or not session.streaming:
        return {"steered": False}
    # M1: queue as a follow-up user message for the next turn.
    session.stored.messages.append({"role": "user", "content": str(text)})
    server.store.save(session.stored)
    return {"steered": True}


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
    session = server.session
    if session is None:
        raise _InvalidParams("no active session")
    text = _require(params, "text")
    if server.halted:
        raise _InvalidParams("daemon is halted (/daemon pause); resume with /daemon resume")
    server.last_user_activity = time.time()
    if session.streaming:
        session.pending_prompts.append(str(text))  # really queued: it runs when the current turn ends
        return {"turn_id": "", "status": "queued"}
    if params.get("background"):
        session.background = True
        session.stored.meta["background"] = True
    if session.background and server.background_paused:
        raise _InvalidParams("background work is paused (restart-storm safe mode); resume with /daemon resume")
    session.stored.model = params.get("model") or session.stored.model
    session.turn_task = asyncio.get_running_loop().create_task(server._run_turn(session, str(text)))
    return {"turn_id": session.turn_task.get_name(), "status": "streaming"}


async def _prompt_background(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``/bg``/Ctrl+B. With ``text``: new background session. Without: hand the running turn off."""
    session = server._session_for(params.get("session_id")) or server.session
    if session is None:
        raise _InvalidParams("no active session")
    text = str(params.get("text") or "").strip()
    if text:
        live = server.start_background(session, text)
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
            "started_at": h.started_at,
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


async def _subagent_tail(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    h = server.subagents.handles.get(str(_require(params, "subagent_id")))
    text = "\n".join(h.tail[-30:]) + (("\n" + h.result) if h and h.done else "") if h else ""
    return {"text": text, "status": h.status if h else "unknown", "done": bool(h and h.done)}


async def _clipboard_paste(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    # M1: no clipboard integration; the TUI pastes text inline instead.
    return {"text": "", "images": [], "files": []}


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
    name, arg = parts[0], parts[1] if len(parts) > 1 else ""
    return await server.dispatch_command(name, arg, session_id)


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
    return {"providers": providers, "model": server.config.default_model}


async def _model_save_key(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    _require(params, "provider")
    _require(params, "key")
    # M1: keys come from config env vars; persisting new keys is a later milestone.
    return {"provider": params.get("provider")}


async def _model_disconnect(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
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
    if key == "full":
        return {"config": redact(server.config.model_dump())}  # provider api_key never goes to clients
    if key == "mtime":
        return {"mtime": 0.0}
    value: Any = server.config
    for part in key.split("."):
        value = getattr(value, part, None) if not isinstance(value, dict) else value.get(part)
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
        return {"ok": True, "key": key, "value": "on" if server.config.display.focus_mode else "off"}
    if key == "model":  # TUI /model <key> and the model picker: switch the session's model key
        return await _config_set_model(server, params)
    if "." not in key:
        raise _InvalidParams(f"unsupported config key: {key}")
    section, field_name = key.split(".", 1)
    section_obj = getattr(server.config, section, None)
    if section_obj is None or not hasattr(section_obj, field_name):
        raise _InvalidParams(f"unknown config path: {key}")
    setattr(section_obj, field_name, params.get("value"))
    return {"ok": True, "key": key}


async def _config_set_model(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    """``config.set model``: value is ``<model-key> [--provider p] [--session|--global]`` (picker flags ignored)."""
    parts = str(params.get("value") or "").split()
    keys: list[str] = []
    skip = False
    for part in parts:
        if skip:
            skip = False
        elif part == "--provider":
            skip = True
        elif not part.startswith("--"):
            keys.append(part)
    if not keys:
        raise _InvalidParams("model key required")
    key = keys[0]
    known = {m for p in server.config.providers for m in p.models} | {server.config.default_model}
    if key not in known:
        raise _InvalidParams(f"unknown model key: {key} (known: {', '.join(sorted(known))})")
    session = server._session_for(params.get("session_id"))
    if session is None:
        server.config.default_model = key
    else:
        session.stored.model = key
        server.store.save(session.stored)
        session.emit("session.info", session.live_info())
    return {"ok": True, "key": "model", "value": key}


async def _setup_status(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_configured": bool(server.config.providers),
        "ready": bool(server.config.providers),
    }


async def _system_battery(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    return {"available": False}


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
    expanded = Path(raw).expanduser()
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
    "model.save_key": _model_save_key,
    "model.disconnect": _model_disconnect,
    "config.get": _config_get,
    "config.set": _config_set,
    "setup.status": _setup_status,
    "system.battery": _system_battery,
    "browser.manage": _browser_manage,
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
