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
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from k3code.agent.loop import AgentLoop, ApprovalResult
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
from k3code.router import CooldownStore, Router, RouterEvent, build_chain

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
        self.server.emit("session.info", self.live_info())

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
        }


class GatewayServer:
    """Owns the stdio loop, the live session, the router and the session store."""

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
        self.session: LiveSession | None = None
        self.providers: list[Any] = []
        self.router: Router | None = None
        self.cooldowns = CooldownStore()
        self._running = False
        self._server_request_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}

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
        import os

        return Path(os.environ.get("K3CODE_HOME", str(Path.home() / ".k3code"))).expanduser()

    # ── transport ─────────────────────────────────────────────────────

    def _write(self, line: str) -> None:
        """One JSON frame to stdout. Only frames ever land here."""
        out = self._stdout
        if out is None:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        elif hasattr(out, "write"):
            out.write(line + "\n")
            out.flush()

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Send a server→client event frame."""
        self._write(encode_event(event_type, payload))

    # ── lifecycle ─────────────────────────────────────────────────────

    async def serve(self) -> None:
        """Read frames from stdin until EOF, dispatching each."""
        self._running = True
        loop = asyncio.get_running_loop()
        reader = self._stdin
        if reader is None:
            reader = asyncio.StreamReader()
            await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

        self._send_ready()

        while self._running:
            line = await reader.readline()
            if not line:
                logger.info("stdin EOF; gateway exiting")
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                await self._handle_line(text)
            except Exception:
                logger.exception("unhandled error processing frame")

        self.shutdown()

    def shutdown(self) -> None:
        self._running = False
        if self.session is not None:
            self.session.pending_approval = None
        for fut in self._server_request_futures.values():
            if not fut.done():
                fut.cancel()
        self._server_request_futures.clear()

    async def close(self) -> None:
        for p in self.providers:
            await p.aclose()
        self.store.close()

    def _send_ready(self) -> None:
        self.emit(
            "gateway.ready",
            {"skin": _DEFAULT_SKIN, "change_events": [], "replay_epoch": 1},
        )

    # ── frame handling ────────────────────────────────────────────────

    async def _handle_line(self, text: str) -> None:
        obj, err = decode_frame(text)
        if err is not None:
            kind = PARSE_ERROR if err.startswith("parse error") else INVALID_REQUEST
            self._write(encode_error(None, kind, err))
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
            self._write(encode_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}"))
            return

        try:
            result = await handler(self, params)
        except _InvalidParams as e:
            self._write(encode_error(req_id, INVALID_PARAMS, str(e)))
        except Exception as e:  # noqa: BLE001 - one bad method must not kill the gateway
            logger.exception("method %s failed", method)
            self._write(encode_error(req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}"))
        else:
            self._write(encode_response(req_id, result))

    def _resolve_server_request(self, req_id: Any, obj: dict[str, Any]) -> None:
        fut = self._server_request_futures.pop(str(req_id), None)
        if fut is None or fut.done():
            return
        if "error" in obj:
            fut.set_exception(RuntimeError(str(obj["error"].get("message", "client error"))))
        else:
            fut.set_result(obj.get("result") or {})

    # ── server→client requests ────────────────────────────────────────

    async def _ask_client(self, method: str, params: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Send an approval/clarify/sudo/secret request and await the client's result."""
        req_id = next_request_id(method)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._server_request_futures[req_id] = fut
        params = {"session_id": session_id, **params}
        self._write(encode_server_request(method, params, req_id=req_id))
        try:
            return await fut
        except asyncio.CancelledError:
            # The turn was interrupted while waiting: tell the client to stop showing it.
            self.emit(
                "notification.show",
                {"text": "Request cancelled", "level": "info", "kind": "info", "key": req_id},
            )
            self._write(
                encode_server_request(
                    "request.cancel",
                    {"id": req_id, "method": method, "reason": "interrupted"},
                )
            )
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
        chain = build_chain(self.providers, _resolve_model_specs(self.config, key))
        self.cooldowns = CooldownStore()
        self.router = Router(chain, cooldowns=self.cooldowns, on_event=self._on_router_event)
        self._chain_key = key

    _chain_key: str | None = None

    def _on_router_event(self, event: RouterEvent) -> None:
        if event.kind == "router.failover":
            self.emit(
                "status.update",
                {
                    "kind": "status",
                    "text": f"failover: {event.provider}/{event.model} ({event.reason})",
                },
            )
        elif event.kind == "router.exhausted":
            self.emit("error", {"message": f"All providers failed: {event.detail}"})

    # ── turn lifecycle ────────────────────────────────────────────────

    async def _run_turn(self, session: LiveSession, text: str) -> None:
        """Execute one user prompt end-to-end, emitting wire events."""
        self._ensure_router(session.stored.model or None)
        assert self.router is not None
        session.perms.cwd = Path(session.stored.cwd or Path.cwd())  # session cwd, never the process cwd
        session.perms.reload()
        loop = AgentLoop(
            self.router,
            system_prompt=session.system_prompt,
            max_turns=self.config.max_turns,
            headless=False,
            on_event=self._on_router_event,
            cwd=session.perms.cwd,
            approval_callback=await self._approval_callback_for(session),
            plan_callback=self._plan_callback_for(session),
            on_auto_allow=lambda tool, args, dec: self.emit(
                "permission.auto_allowed",
                {"session_id": session.session_id, "tool": tool, "command": _command_for_tool(tool, args)},
            ),
            permissions=session.perms,
        )
        session.loop = loop

        config = self.config
        final_text = ""
        usage = Usage()
        error: str | None = None
        status = "done"

        async def on_text_delta(chunk: str) -> None:
            nonlocal final_text
            final_text += chunk
            self.emit("message.delta", {"text": chunk})

        loop.on_text_delta = on_text_delta

        self.emit("message.start", {})
        self.emit("status.update", {"kind": "status", "text": "thinking"})
        session.streaming = True
        try:
            async for event in loop.run(
                text,
                model=session.stored.model or None,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                history=session.history,
            ):
                self._on_stream_event(session, event)
        except (AllProvidersUnreachable, ChainExhausted, ContextOverflow) as e:
            status = "error"
            error = str(e)
            self.emit("error", {"message": str(e)})
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("turn failed")
            status = "error"
            error = str(e)
            self.emit("error", {"message": str(e)})
        finally:
            session.streaming = False

        # Persist whatever the loop accumulated (also on error/interrupt).
        if loop.turn_messages:
            session.stored.messages = _serialize_messages(loop.turn_messages)
            session.stored.model = session.stored.model or self._chain_key or self.config.default_model
            self.store.save(session.stored)
        for msg in reversed(loop.turn_messages):
            if msg.role == "assistant" and msg.usage:
                usage = msg.usage
                break

        self.emit(
            "message.complete",
            {"text": final_text, "usage": _usage_payload(usage), "status": status, "error": error},
        )
        self.emit("status.update", {"kind": "status", "text": ""})

    def _on_stream_event(self, session: LiveSession, event: StreamEvent) -> None:
        if event.type == "tool_call" and event.tool_call:
            self.emit(
                "tool.generating",
                {"name": event.tool_call.name},
            )
            self.emit(
                "tool.start",
                {
                    "tool_id": event.tool_call.id,
                    "name": event.tool_call.name,
                    "args": event.tool_call.arguments,
                },
            )
        elif event.type == "done" and event.message:
            msg = event.message
            if msg.role == "tool":
                self.emit(
                    "tool.complete",
                    {
                        "tool_id": msg.tool_call_id or "",
                        "name": msg.name or "",
                        "result_text": msg.content or "",
                        "result": {"content": msg.content},
                    },
                )
            elif msg.usage:
                self.emit("session.usage", {"usage": _usage_payload(msg.usage)})

    async def _approval_callback_for(self, session: LiveSession) -> Any:
        async def approve(tool_name: str, arguments: dict[str, Any], decision: Any = None) -> ApprovalResult:
            decision = decision or session.perms.decide(tool_name, arguments)
            rules = suggest_rules(tool_name, decision)
            pattern = ", ".join(r.pattern for r in rules)
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
        session.turn_task.cancel()
        return True

    def _session_for(self, session_id: str | None) -> LiveSession | None:
        if self.session is None:
            return None
        if session_id and session_id != self.session.session_id:
            return None
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
    server.session = LiveSession(stored.session_id, stored, server)
    server.session.reasoning_effort = params.get("effort")
    return {"session_id": stored.session_id, "info": server.session.live_info()}


async def _session_list(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    limit = int(params.get("limit") or 50)
    rows = []
    for s in server.store.list(limit=limit):
        preview = ""
        for m in s.messages:
            if m.get("role") == "user" and m.get("content"):
                preview = str(m["content"])[:120]
                break
        rows.append(
            {
                "id": s.session_id,
                "title": s.title or preview or "Session",
                "preview": preview,
                "started_at": s.created_at,
                "message_count": len(s.messages),
            }
        )
    return {"sessions": rows}


async def _session_active_list(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    s = server.session
    if s is not None:
        items.append(
            {
                "current": True,
                "id": s.session_id,
                "last_active": s.stored.updated_at,
                "message_count": len(s.stored.messages),
                "model": s.stored.model,
                "preview": (s.stored.title or "")[:120],
                "session_key": s.session_id,
                "started_at": s.stored.created_at,
                "status": "streaming" if s.streaming else "idle",
                "title": s.stored.title or "Session",
            }
        )
    return {"sessions": items}


async def _session_resume(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    stored = server.store.get(str(sid))
    if stored is None:
        raise _InvalidParams(f"unknown session: {sid}")
    server.session = LiveSession(stored.session_id, stored, server)
    server.emit("session.resume_progress", {"phase": "done", "status": "done", "message_count": len(stored.messages)})
    return {
        "session_id": stored.session_id,
        "message_count": len(stored.messages),
        "messages": stored.messages,
        "info": server.session.live_info(),
    }


async def _session_activate(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    stored = server.store.get(str(sid))
    if stored is None:
        raise _InvalidParams(f"unknown session: {sid}")
    server.session = LiveSession(stored.session_id, stored, server)
    return {"session_id": stored.session_id, "info": server.session.live_info()}


async def _session_delete(server: GatewayServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = _require(params, "session_id")
    deleted = server.store.delete(str(sid))
    if server.session is not None and server.session.session_id == sid:
        server.session = None
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
