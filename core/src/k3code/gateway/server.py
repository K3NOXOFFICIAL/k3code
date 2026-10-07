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
from k3code.goals import GoalManager, make_judge
from k3code.learning.hub import LearningHub
from k3code.mcpclient import McpManager
from k3code.paths import project_config_path as _proj_cfg
from k3code.paths import user_config_path as _user_cfg
from k3code.permissions import MODE_CYCLE_NAMES, PermissionMode, suggest_rules
from k3code.permissions.state import PermissionState, persist_rules, project_config_path
from k3code.prompting import build_system_prompt
from k3code.providers import make_providers
from k3code.providers.types import Message, StreamEvent, Usage
from k3code.redact import redact
from k3code.reliability import BudgetExceeded, DiskGuardFull, Reliability, build_reliability
from k3code.reliability import events as rev
from k3code.reliability.persistent_retry import TurnCancelled
from k3code.research.flow import Research
from k3code.research.tools import register_web_tools
from k3code.router import CooldownStore, Router, RouterEvent, build_chain
from k3code.routing.caller import ModelCaller
from k3code.routing.tiers import Escalation, TaskKind, Tier, TierRouters, router_options, tier_for
from k3code.session_ai import make_title
from k3code.subagents import SubagentManager
from k3code.subagents.tools import register_task_tools
from k3code.usage import UsageDB

logger = logging.getLogger("k3code.gateway")

#: Emitted for gateway.ready; the TUI repaints its palette from this.
_DEFAULT_SKIN = {
    "name": "k3code",
    "tool_prefix": "k3",
}

_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system.md"


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
        self.reasoning_effort: str | None = None
        self.todos: list[dict[str, Any]] = []
        self.todo_revision = 0
        self.pending_approval: asyncio.Future[dict[str, Any]] | None = None
        self.pending_request_id: str | None = None
        self.perms = PermissionState(
            mode=PermissionMode(stored.meta.get("mode") or server.config.permission_mode),
            cwd=Path(stored.cwd or Path.cwd()),
            add_dirs=list(stored.meta.get("add_dirs") or []),
        )
        self.control: dict[str, Any] = {"goal": "", "loop": "", "heartbeat": "", "revision": 0, "updated_at": 0.0}
        if stored.meta.get("goal"):
            from k3code.goals import GoalState

            self.control["goal"] = GoalState.from_dict(stored.meta["goal"]).snapshot()
        #: Background/cron/loop sessions run bash inside the sandbox.
        self.background = bool(stored.meta.get("background"))
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
        if self.needs_input:
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


class Client:
    """One attached JSON-RPC peer (the stdio pipe, or one Unix-socket connection)."""

    def __init__(self, send: Callable[[str], None], name: str = "stdio") -> None:
        self.send = send
        self.name = name
        self.session_id: str | None = None
        self.closed = False


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
        self._client_seq = 0
        self.usage = UsageDB(self._home() / "usage.db")
        self.artifacts = ArtifactStore(self._home() / "artifacts.db")
        self._tiers: TierRouters | None = None
        self._side_tasks: set[asyncio.Task[Any]] = set()  # fire-and-forget work (titles)
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

    def attach(self, client: Client, live: LiveSession) -> None:
        """Point ``client`` at ``live`` and replay any approvals it is still waiting on."""
        client.session_id = live.session_id
        for req_id, (sid, frame) in list(self._open_requests.items()):
            if sid == live.session_id:
                client.send(frame)
                logger.debug("re-sent open request %s to %s", req_id, client.name)

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
            try:
                await self._handle_line(text)
            except Exception:
                logger.exception("unhandled error processing frame")

    async def start_socket(self, path: Path | str) -> None:
        """Listen on a Unix socket: one JSON-RPC connection per client, sessions shared."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()  # stale socket from a crashed daemon
        self._socket_server = await asyncio.start_unix_server(self._on_connect, path=str(path))
        os.chmod(path, 0o600)
        self.socket_path = path

    async def stop_socket(self) -> None:
        if self._socket_server is not None:
            self._socket_server.close()
            with contextlib.suppress(Exception):
                await self._socket_server.wait_closed()
            self._socket_server = None
        with contextlib.suppress(Exception):
            Path(getattr(self, "socket_path", "")).unlink()

    socket_path: Path

    def request_stop(self) -> None:
        self._stop.set()

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def send(line: str) -> None:
            writer.write((line + "\n").encode("utf-8"))

        self._client_seq += 1
        client = Client(send, name=f"socket#{self._client_seq}")
        self.clients.append(client)
        logger.info("client %s attached", client.name)
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
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    await self._handle_line(text, client)
                except Exception:
                    logger.exception("unhandled error processing frame")
                with contextlib.suppress(Exception):
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            client.closed = True
            if client in self.clients:
                self.clients.remove(client)
            logger.info("client %s detached; its sessions keep running", client.name)
            with contextlib.suppress(Exception):
                writer.close()

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
        if self.automation is not None:
            await self.automation.stop()
        for live in self.live.values():
            if live.turn_task is not None and not live.turn_task.done():
                live.turn_task.cancel()
            if live.reliability is not None:
                with contextlib.suppress(Exception):
                    await live.reliability.stop()
        await self.mcp.close()
        for p in self.providers:
            await p.aclose()
        self.store.close()
        self.usage.close()

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
        for client in self.clients:
            if client.session_id == session_id:
                self._send(client, frame)
        try:
            return await fut
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

    # ── router wiring ─────────────────────────────────────────────────

    def _ensure_router(self, model: str | None = None) -> None:
        """Build provider chain + router once (rebuilt on model change)."""
        key = model or self.config.default_model
        if self.router is not None and self._chain_key == key:
            return
        # Old provider clients are dropped for GC; chains are only replaced on
        # a model change (rare), and httpx pools close with the objects.
        self.providers = make_providers(self.config.providers)
        self.cooldowns = CooldownStore(path=self._home() / "cooldowns.json")
        self._tiers = TierRouters(
            self.providers, self.config, cooldowns=self.cooldowns, on_event=self._on_router_event, main_key=key
        )
        self.router = self._tiers.get(Tier.MAIN)
        self._chain_key = key

    _chain_key: str | None = None

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
        _ctx_session.set(session)  # this task's events belong to the session, not to the requesting client
        prompt = text
        mgr = self.goal_manager(session)
        while True:
            try:
                status, final_text = await self._run_one_turn(session, prompt)
            except asyncio.CancelledError:
                if mgr.is_active():
                    mgr.pause("interrupted")
                    self.emit_goal(session)
                raise
            if status != "done" or not mgr.is_active():
                if status == "error" and mgr.is_active():
                    mgr.pause("turn failed")
                    self.emit_goal(session)
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
            task_kind=kind.value,
            max_tool_errors=max_tool_errors,
        )
        register_skill_tool(loop.tools, session.perms.cwd, list(self.config.skills.roots))
        register_mcp_tools(loop.tools, self.mcp)
        for install in session.extra_tools:
            install(loop.tools)
        register_task_tools(loop.tools, self, session, depth=1)
        register_web_tools(loop.tools, self.config)
        return loop

    async def _run_one_turn(self, session: LiveSession, text: str) -> tuple[str, str]:
        """Execute one prompt end-to-end, emitting wire events. Returns (status, final_text)."""
        self._ensure_router(session.stored.model or None)
        assert self.router is not None
        session.perms.cwd = Path(session.stored.cwd or Path.cwd())  # session cwd, never the process cwd
        session.perms.reload()
        session.needs_input = False
        session.run_result = None
        session.last_error, session.last_exc, session.last_api_calls = "", None, 0
        reliability = await self._reliability_for(session)
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

        loop.on_text_delta = on_text_delta

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
            if session.paused:  # a cancelled wait never saw "resumed"
                session.paused = False
                session.emit("notification.clear", {"key": self.PAUSE_KEY}, importance="essential")
            self.autonomy.finish(session, gate, status, final_text, text)
            if self.learning.enabled:
                self.learning.spawn(self.learning.turn_finished(session, status))

        # Persist whatever the loop accumulated (also on error/interrupt).
        if loop.turn_messages:
            session.stored.messages = _serialize_messages(loop.turn_messages)
            session.stored.model = session.stored.model or self._chain_key or self.config.default_model
            self.store.save(session.stored)
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
        msg = await router.complete(messages, [], model=model_key, max_tokens=max_tokens)
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
            entry["tool_calls"] = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls]
        out.append(entry)
    return out


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


async def _session_resume(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    stored = server.store.get(str(sid))
    if stored is None:
        raise _InvalidParams(f"unknown session: {sid}")
    live = server.live_for(stored)  # a background session keeps its running state
    server.session = live
    server.emit("session.resume_progress", {"phase": "done", "status": "done", "message_count": len(stored.messages)})
    return {
        "session_id": stored.session_id,
        "message_count": len(live.stored.messages),
        "messages": live.stored.messages,
        "info": live.live_info(),
    }


async def _session_activate(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    stored = server.store.get(str(sid))
    if stored is None:
        raise _InvalidParams(f"unknown session: {sid}")
    live = server.live_for(stored)
    server.session = live
    return {"session_id": stored.session_id, "info": live.live_info()}


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
    server.last_user_activity = time.time()
    if session.streaming:
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
    if "." not in key:
        raise _InvalidParams(f"unsupported config key: {key}")
    section, field_name = key.split(".", 1)
    section_obj = getattr(server.config, section, None)
    if section_obj is None or not hasattr(section_obj, field_name):
        raise _InvalidParams(f"unknown config path: {key}")
    setattr(section_obj, field_name, params.get("value"))
    return {"ok": True, "key": key}


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


_HANDLERS: dict[str, Any] = {
    "session.create": _session_create,
    "session.list": _session_list,
    "session.active_list": _session_active_list,
    "session.resume": _session_resume,
    "session.activate": _session_activate,
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
