"""MCP client: stdio + streamable-HTTP servers from ``mcp.servers`` config.

Each server runs in its own background task that owns the SDK's anyio context
managers (they must be entered and exited from one task) and serves tool calls
through a queue. Tools are exposed as ``mcp__<server>__<tool>``; their schemas
stay deferred (only names go into the prompt) until ``mcp_tool_search`` loads them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from k3code.config import McpServerConfig, env_value

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
#: A runner that failed (or whose server died) is restarted on a later turn, at most this often.
RESTART_BACKOFF_S = 30.0
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def qualified_name(server: str, tool: str) -> str:
    return f"mcp__{_SAFE.sub('_', server)}__{_SAFE.sub('_', tool)}"


def request_headers(cfg: McpServerConfig) -> dict[str, str]:
    """Headers for an HTTP MCP server: the configured ones, plus a bearer token read from ``bearer_env`` now."""
    headers = dict(cfg.headers)
    if cfg.bearer_env:
        token = env_value(cfg.bearer_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


@dataclass
class McpToolInfo:
    server: str
    name: str  # server-side tool name
    description: str
    schema: dict[str, Any]

    @property
    def qualified(self) -> str:
        return qualified_name(self.server, self.name)


@dataclass
class McpServerState:
    name: str
    transport: str
    status: str = "pending"  # pending | connected | failed | disabled
    error: str = ""
    tools: list[McpToolInfo] = field(default_factory=list)


class _Runner:
    def __init__(self, name: str, cfg: McpServerConfig) -> None:
        self.name = name
        self.cfg = cfg
        self.state = McpServerState(name, "http" if cfg.url else "stdio")
        self.ready = asyncio.Event()
        self.dead = asyncio.Event()  # set when the server connection broke
        self.queue: asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future[Any]] | None] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.started_at = 0.0  # monotonic time of the last start (restart back-off)

    async def _call(self, session: Any, tool: str, args: dict[str, Any], fut: asyncio.Future[Any]) -> None:
        """One tool call. The caller may have been cancelled meanwhile (/stop, a timeout): its future is then done and
        resolving it raises InvalidStateError, which used to kill the runner for EVERY session."""
        try:
            res = await asyncio.wait_for(session.call_tool(tool, args), CALL_TIMEOUT)
            if not fut.done():
                fut.set_result(res)
        except Exception as e:  # noqa: BLE001
            if not fut.done():
                fut.set_exception(e)
            if _is_transport_error(e):
                self.state.status = "failed"  # the server is gone: stop advertising it as connected
                self.state.error = _describe(e)
                self.dead.set()

    async def _run(self) -> None:
        from mcp import ClientSession

        try:
            async with contextlib.AsyncExitStack() as stack:
                if self.cfg.url:
                    import httpx
                    from mcp.client.streamable_http import streamable_http_client  # type: ignore[attr-defined]

                    http = httpx.AsyncClient(headers=request_headers(self.cfg) or None, timeout=CALL_TIMEOUT)
                    await stack.enter_async_context(http)
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(self.cfg.url, http_client=http)
                    )
                else:
                    from mcp import StdioServerParameters
                    from mcp.client.stdio import stdio_client

                    if not self.cfg.command:
                        raise ValueError("server needs `command` (stdio) or `url` (http)")
                    params = StdioServerParameters(
                        command=self.cfg.command,
                        args=self.cfg.args,
                        env={**os.environ, **self.cfg.env} if self.cfg.env else None,
                        cwd=self.cfg.cwd,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
                listed = await asyncio.wait_for(session.list_tools(), CONNECT_TIMEOUT)
                self.state.tools = [
                    McpToolInfo(
                        self.name, t.name, t.description or "", dict(_attr(t, "input_schema", "inputSchema") or {})
                    )
                    for t in listed.tools
                ]
                self.state.status = "connected"
                self.ready.set()
                inflight: set[asyncio.Task[None]] = set()
                get_next: asyncio.Task[Any] | None = None
                try:
                    while True:
                        get_next = asyncio.ensure_future(self.queue.get())
                        dead = asyncio.ensure_future(self.dead.wait())
                        done, _ = await asyncio.wait({get_next, dead}, return_when=asyncio.FIRST_COMPLETED)
                        dead.cancel()
                        if get_next not in done:  # the connection broke while idle
                            get_next.cancel()
                            raise RuntimeError(self.state.error or "mcp connection lost")
                        req = get_next.result()
                        if req is None:
                            break
                        tool, args, fut = req
                        if fut.done():  # the caller gave up while the request was queued
                            continue
                        call = asyncio.create_task(self._call(session, tool, args, fut))
                        inflight.add(call)  # concurrent: a slow call must not block the other sessions
                        call.add_done_callback(inflight.discard)
                finally:
                    if get_next is not None and not get_next.done():
                        get_next.cancel()
                    for call in list(inflight):
                        call.cancel()
        except BaseException as e:  # noqa: BLE001 - includes SDK ExceptionGroups
            if self.state.status != "connected" or not isinstance(e, asyncio.CancelledError):
                self.state.status = "failed"
                self.state.error = _describe(e)
            logger.info("mcp server %s stopped: %s", self.name, self.state.error)
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self.ready.set()
            # fail anything still queued
            while not self.queue.empty():
                req = self.queue.get_nowait()
                if req is not None and not req[2].done():
                    req[2].set_exception(RuntimeError(f"mcp server {self.name} is not running"))

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self._run(), name=f"mcp-{self.name}")

    async def stop(self) -> None:
        if self.task is None:
            return
        if not self.task.done():
            self.queue.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self.task.cancel()
                with contextlib.suppress(BaseException):
                    await self.task
        self.task = None


def _attr(obj: Any, *names: str) -> Any:
    """First present attribute (mcp 2.x snake_case, mcp 1.x camelCase)."""
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


def _is_transport_error(e: BaseException) -> bool:
    """The connection to the server is gone (as opposed to the tool failing)."""
    if isinstance(e, BaseExceptionGroup):
        return any(_is_transport_error(x) for x in e.exceptions)
    name = type(e).__name__
    text = str(e).lower()
    return (
        isinstance(e, (BrokenPipeError, ConnectionError, EOFError))
        or name in ("ClosedResourceError", "BrokenResourceError", "EndOfStream")
        or "connection closed" in text
        or "server disconnected" in text
    )


def _describe(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup) and e.exceptions:
        return _describe(e.exceptions[0])
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


class McpManager:
    """All configured MCP servers; ``ensure_started`` is idempotent, ``reload`` restarts from config."""

    def __init__(self, servers: dict[str, McpServerConfig] | None = None) -> None:
        self.servers = dict(servers or {})
        self._runners: dict[str, _Runner] = {}
        self._started = False
        self._stale = False
        self._lock = asyncio.Lock()  # ensure_started runs on every turn of every session: single-flight

    def configure(self, servers: dict[str, McpServerConfig]) -> None:
        """Adopt new server definitions; running servers are restarted on the next ``ensure_started``."""
        if {k: v.model_dump() for k, v in servers.items()} != {k: v.model_dump() for k, v in self.servers.items()}:
            self._stale = True
        self.servers = dict(servers)

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._started and self._stale:
                await self._close()
            if self._started:
                await self._revive()
                return
            self._stale = False
            self._started = True
            for name, cfg in self.servers.items():
                self._runners[name] = self._launch(name, cfg)
            await self._await_ready()

    def _launch(self, name: str, cfg: McpServerConfig) -> _Runner:
        runner = _Runner(name, cfg)
        if not cfg.enabled:
            runner.state.status = "disabled"
            runner.ready.set()
            return runner
        runner.started_at = time.monotonic()
        runner.start()
        return runner

    async def _await_ready(self) -> None:
        await asyncio.gather(
            *(asyncio.wait_for(r.ready.wait(), CONNECT_TIMEOUT + 5) for r in self._runners.values()),
            return_exceptions=True,
        )
        for r in self._runners.values():
            if r.state.status == "pending":
                r.state.status = "failed"
                r.state.error = "connect timeout"

    async def _revive(self) -> None:
        """Restart servers that failed to start or died, at most once per RESTART_BACKOFF_S each. ensure_started used
        to return as soon as it had run once, so a server that was down at daemon boot (network not up yet) or that
        crashed later stayed broken until `/mcp reload`."""
        now = time.monotonic()
        for name, r in list(self._runners.items()):
            gone = r.task is None or r.task.done() or r.dead.is_set() or r.state.status == "failed"
            if not r.cfg.enabled or not gone or now - r.started_at < RESTART_BACKOFF_S:
                continue
            logger.info("restarting mcp server %s (%s)", name, r.state.error or r.state.status)
            await r.stop()
            self._runners[name] = self._launch(name, r.cfg)
        await self._await_ready()

    async def reload(self, servers: dict[str, McpServerConfig] | None = None) -> None:
        await self.close()
        if servers is not None:
            self.servers = dict(servers)
        await self.ensure_started()

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        await asyncio.gather(*(r.stop() for r in self._runners.values()), return_exceptions=True)
        self._runners.clear()
        self._started = False

    def states(self) -> list[McpServerState]:
        return [r.state for r in self._runners.values()]

    def tools(self) -> list[McpToolInfo]:
        return [t for r in self._runners.values() for t in r.state.tools]

    def find(self, qualified: str) -> McpToolInfo | None:
        return next((t for t in self.tools() if t.qualified == qualified), None)

    async def call(self, qualified: str, args: dict[str, Any]) -> dict[str, Any]:
        info = self.find(qualified)
        if info is None:
            return {"error": f"Unknown MCP tool: {qualified}"}
        runner = self._runners[info.server]
        if runner.state.status != "connected":
            return {"error": f"MCP server {info.server} is {runner.state.status}: {runner.state.error}"}
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        runner.queue.put_nowait((info.name, args, fut))
        try:
            res = await fut
        except Exception as e:  # noqa: BLE001
            return {"error": f"MCP call failed: {_describe(e)}"}
        text = "\n".join(getattr(c, "text", None) or f"[{getattr(c, 'type', 'content')}]" for c in res.content)
        return {"error": text or "MCP tool error"} if _attr(res, "is_error", "isError") else {"content": text}

    def search(self, query: str, limit: int = 8) -> list[McpToolInfo]:
        """Tools whose qualified name/description match ``query`` (exact names first)."""
        q = query.strip().lower()
        words = [w for w in re.split(r"[\s,]+", q) if w]
        scored = []
        for t in self.tools():
            hay = f"{t.qualified} {t.description}".lower()
            if q == t.qualified.lower() or q == t.name.lower():
                score = 100
            else:
                score = sum(hay.count(w) for w in words)
            if score:
                scored.append((score, t))
        scored.sort(key=lambda p: (-p[0], p[1].qualified))
        return [t for _, t in scored[:limit]]
