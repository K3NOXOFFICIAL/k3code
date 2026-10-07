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

from k3code.agent.loop import AgentLoop, ApprovalResult
from k3code.autonomy import autonomy_cfg
from k3code.autonomy.plan_first import GateResult, PlanFirst
from k3code.commands import CommandRegistry
from k3code.commands.builtin import build_registry as build_commands
from k3code.config import Settings, load_config
from k3code.errors import AllProvidersUnreachable, ChainExhausted, ContextOverflow
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
from k3code.permissions import MODE_CYCLE_NAMES, PermissionMode, suggest_rules
from k3code.permissions.state import PermissionState, log_decision, persist_rules, project_config_path
from k3code.providers import make_providers
from k3code.providers.types import Message, StreamEvent, Usage
from k3code.reliability import BudgetExceeded, DiskGuardFull, Reliability, build_reliability
from k3code.reliability import events as rev
from k3code.reliability.persistent_retry import TurnCancelled
from k3code.router import CooldownStore, Router, RouterEvent
from k3code.routing.caller import ModelCaller
from k3code.routing.tiers import Escalation, TaskKind, Tier, TierRouters, tier_for
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
        self.system_prompt = _load_system_prompt()
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
        self.control = {"goal": "", "loop": "", "heartbeat": "", "revision": 0, "updated_at": 0.0}
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

    @property
    def state(self) -> str:
        """``working`` (also while paused), ``needs_input`` or ``idle``."""
        if self.needs_input:
            return "needs_input"
        return "working" if self.streaming else "idle"

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
        self.providers: list[Any] = []
        self.router: Router | None = None
        self.cooldowns = CooldownStore()
        self._running = False
        self._server_request_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        #: Server→client requests still unanswered: id → (session_id, frame). Re-sent on attach.
        self._open_requests: dict[str, tuple[str, str]] = {}
        self._stdio_client = Client(lambda line: self._write(line))
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
        self._tiers: TierRouters | None = None
        #: (provider, model) of the most recent router attempt on any task (side calls read it).
        self.last_attempt: tuple[str, str] = ("", "")
        self.model_caller = ModelCaller(
            self.tier_routers, self.config, self.usage, emit=lambda t, p: self.emit(t, p),
            last_attempt=lambda: self.last_attempt,
        )
        self.autonomy = PlanFirst(self)

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

    def _write(self, line: str) -> None:
        """One JSON frame to stdout (the stdio client's sink). Only frames ever land here."""
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
        record = {"ts": time.time(), "type": event_type, "session": sess.session_id if sess else None,
                  "importance": importance, "payload": payload}
        self.event_log.append(record)
        if self.debug:
            logger.info("event %s", json.dumps({k: v for k, v in record.items() if k != "ts"}, default=str)[:2000])
        line = encode_event(event_type, payload, importance)
        for client in self._targets(session):
            self._send(client, line)

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
                    "title": s.stored.title or "Session",
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
        for live in self.live.values():
            if live.turn_task is not None and not live.turn_task.done():
                live.turn_task.cancel()
            if live.reliability is not None:
                with contextlib.suppress(Exception):
                    await live.reliability.stop()
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
            cancel = encode_server_request(
                "request.cancel", {"id": req_id, "method": method, "reason": "interrupted"}
            )
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
        self.cooldowns = CooldownStore()
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
                until = time.strftime("%H:%M", time.localtime(time.time() + float(data.get("delay") or 0)))
                text = f"⏸ waiting for provider until {until}"
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

    def _build_loop(
        self, session: LiveSession, reliability: Reliability, router: Router, kind: TaskKind, approval: Any,
        *, max_tool_errors: int = 0,
    ) -> AgentLoop:
        return AgentLoop(
            router,
            system_prompt=session.system_prompt,
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

    async def _run_turn(self, session: LiveSession, text: str) -> None:
        """Execute one user prompt end-to-end, emitting wire events to the session's clients."""
        _ctx_session.set(session)  # this task's events belong to the session, not to the requesting client
        self._ensure_router(session.stored.model or None)
        assert self.router is not None
        session.perms.cwd = Path(session.stored.cwd or Path.cwd())  # session cwd, never the process cwd
        session.perms.reload()
        session.needs_input = False
        reliability = await self._reliability_for(session)
        approval = await self._approval_callback_for(session)

        # M4a: which tier runs this turn; cheap/fast tiers escalate when the attempt stalls.
        kind = TaskKind.BACKGROUND_TURN if session.background else TaskKind.INTERACTIVE_TURN
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
        loop = self._build_loop(session, reliability, self.tier_routers().get(tier), kind, approval,
                                max_tool_errors=max_errors)
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
            if not gate.proceed:
                final_text = gate.message
                session.emit("message.delta", {"text": gate.message})
            while gate.proceed:
                use_model = (session.stored.model or None) if tier is Tier.MAIN else None
                async for event in loop.run(
                    prompt,
                    model=use_model,
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
                loop = self._build_loop(session, reliability, self.tier_routers().get(tier), kind, approval,
                                        max_tool_errors=max_errors)
                loop.on_text_delta = on_text_delta
                session.loop = loop
        except (AllProvidersUnreachable, ChainExhausted, ContextOverflow, DiskGuardFull) as e:
            status = "error"
            error = str(e)
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
            session.emit("error", {"message": str(e)})
        finally:
            session.streaming = False
            if session.paused:  # a cancelled wait never saw "resumed"
                session.paused = False
                session.emit("notification.clear", {"key": self.PAUSE_KEY}, importance="essential")
            self.autonomy.finish(session, gate, status, final_text, text)

        # Persist whatever the loop accumulated (also on error/interrupt).
        if loop.turn_messages:
            session.stored.messages = _serialize_messages(loop.turn_messages)
            session.stored.model = session.stored.model or self._chain_key or self.config.default_model
            self.store.save(session.stored)
        for msg in reversed(loop.turn_messages):
            if msg.role == "assistant" and msg.usage:
                usage = msg.usage
                break

        session.emit(
            "message.complete",
            {"text": final_text, "usage": _usage_payload(usage), "status": status, "error": error,
             "state": session.state},
        )
        session.emit("status.update", {"kind": "status", "text": "", "state": session.state})

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
            log_decision(
                session=session.session_id,
                tool=tool_name,
                pattern=pattern,
                choice=choice,
                cwd=str(session.perms.cwd),
                home=self._home(),
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
            log_decision(
                session=session.session_id,
                tool="exit_plan",
                pattern="*",
                choice=choice,
                cwd=str(session.perms.cwd),
                home=self._home(),
            )
            if target:
                session.set_mode(PermissionMode(target))
            return target

        return approve_plan

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
        return str(result.get("choice", "deny")).lower() == "once"

    # ── commands ──────────────────────────────────────────────────────

    async def dispatch_command(self, name: str, arg: str, session_id: str | None) -> dict[str, Any]:
        """Run a slash command; returns a CommandDispatchResult-shaped dict."""
        sid = session_id or (self.session.session_id if self.session else None)
        result = await self.commands.dispatch(self, name, arg, sid)
        if result.get("type") == "exit":
            self._running = False
        return result

    async def interrupt_turn(self, session_id: str | None = None) -> bool:
        """Interrupt the running turn (session.interrupt, /stop)."""
        session = self._session_for(session_id)
        if session is None or session.turn_task is None or session.turn_task.done():
            return False
        session.loop.interrupt() if session.loop else None
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
    return {"sessions": server._active_rows(current.session_id if current else None)}


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
    command = str(_require(params, "command")).strip()
    session_id = params.get("session_id")
    if not command.startswith("/"):
        return {"type": "send", "text": command}
    parts = command[1:].split(None, 1)
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
        return {"config": server.config.model_dump()}
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
    "clipboard.paste": _clipboard_paste,
    "image.attach": _image_attach,
    "image.attach_bytes": _image_attach,
    "image.detach": _image_detach,
    "file.attach": _image_attach,
    "pdf.attach": _image_attach,
    "input.detect_drop": _input_detect_drop,
    "command.dispatch": _command_dispatch,
    "slash.exec": _slash_exec,
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
