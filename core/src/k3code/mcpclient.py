"""MCP client: stdio + streamable-HTTP servers from ``mcp.servers`` config.

Each server runs in its own background task that owns the SDK's anyio context
managers (they must be entered and exited from one task) and serves tool calls
through a queue. Tools are exposed as ``mcp__<server>__<tool>``; their schemas
stay deferred (only names go into the prompt) until ``mcp_tool_search`` loads them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from k3code.config import McpServerConfig, env_value
from k3code.reliability.sandbox import child_env
from k3code.toolnames import wire_name

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
#: A runner that failed (or whose server died) is restarted on a later turn, at most this often.
RESTART_BACKOFF_S = 30.0
#: A turn that finds a dead server relaunches it and waits this long for it (a healthy restart takes well under a
#: second); a server that is still starting then connects in the background and its tools come back on a later turn.
REVIVE_WAIT_S = 3.0
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


#: ``prefix{a, b}``. The prefix starts at the start of a word (or after a brace), so a long word is scanned once: a lazy
#: ``\S*?`` tried from every position of it, and a 20,000 character query took seconds on the event loop.
_BRACES = re.compile(r"(?<![^\s{}])([^\s{}]*)\{([^{}]*)\}")


def _expand_names(query: str) -> str:
    """``query`` lower-cased, without a leading ``select:``, ``prefix{a, b}`` written out as ``prefixa, prefixb``."""
    q = query.strip().lower()
    if q.startswith("select:"):
        q = q[len("select:") :].strip()
    return _BRACES.sub(lambda m: ", ".join(m.group(1) + n.strip() for n in m.group(2).split(",") if n.strip()), q)


_WARNED: set[str] = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning(msg, *args)


def _local_ref(root: dict[str, Any], schema: Any) -> Any:
    """``schema``, or what its ``#/...`` ``$ref`` points at inside ``root`` (a few hops; else left as it is)."""
    for _ in range(5):
        ref = schema.get("$ref") if isinstance(schema, dict) else None
        if not isinstance(ref, str) or not ref.startswith("#/"):
            break
        node: Any = root
        for part in ref[2:].split("/"):
            node = node.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(node, dict) else None
        schema = node
    return schema


def flatten_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` as an object schema without a top-level ``anyOf``/``oneOf``/``allOf`` (Anthropic refuses them, and one
    refused tool fails every request of the session): the object variants' ``properties`` merged, ``required`` kept only
    where every variant has it, ``{"type": "object", "properties": {}}`` when nothing usable is left."""
    variants = [
        v for k in ("anyOf", "oneOf", "allOf") for v in (schema.get(k) if isinstance(schema.get(k), list) else [])
    ]
    out = {k: v for k, v in schema.items() if k not in ("anyOf", "oneOf", "allOf")}
    objects = [
        v
        for v in (_local_ref(schema, v) for v in variants)
        if isinstance(v, dict) and isinstance(v.get("properties"), dict)
    ]
    props: dict[str, Any] = dict(out.get("properties") if isinstance(out.get("properties"), dict) else {})
    for v in objects:
        for name, sub in v["properties"].items():
            props.setdefault(name, sub)
    if not props:
        return {"type": "object", "properties": {}}
    required = [r for r in (out.get("required") if isinstance(out.get("required"), list) else []) if isinstance(r, str)]
    if objects:
        common = set.intersection(*({r for r in v.get("required") or [] if isinstance(r, str)} for v in objects))
        required += [r for r in props if r in common and r not in required]
    out.update(type="object", properties=props)
    out.pop("required", None)
    if required:
        out["required"] = required
    return out


@dataclass
class McpToolInfo:
    server: str
    name: str  # server-side tool name
    description: str
    schema: dict[str, Any]

    def __post_init__(self) -> None:
        schema = self.schema
        if isinstance(schema, dict) and any(isinstance(schema.get(k), list) for k in ("anyOf", "oneOf", "allOf")):
            _warn_once(
                self.full_name, "mcp tool %s: top-level anyOf/oneOf/allOf in its input schema flattened", self.name
            )
            self.schema = flatten_input_schema(schema)
        elif isinstance(schema, dict) and schema and "type" not in schema:
            self.schema = {**schema, "type": "object"}

    @property
    def full_name(self) -> str:
        return qualified_name(self.server, self.name)

    @property
    def qualified(self) -> str:
        """The name the model sees and calls: ``full_name``, shortened (``wire_name``) when it is over 64 characters."""
        return wire_name(self.full_name)

    def problem(self) -> str:
        """Why this tool cannot be advertised (empty when it can)."""
        if not isinstance(self.name, str) or not self.name.strip():
            return "empty tool name"
        if not isinstance(self.schema, dict) or self.schema.get("type", "object") != "object":
            return "input schema is not an object schema"
        if "properties" in self.schema and not isinstance(self.schema["properties"], dict):
            return "input schema properties is not an object"
        return ""


@dataclass
class McpServerState:
    name: str
    transport: str
    status: str = "pending"  # pending | connected | failed | disabled
    error: str = ""
    tools: list[McpToolInfo] = field(default_factory=list)


def stdio_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Environment for a stdio MCP server: the scrubbed daemon set plus the server's configured ``env``.

    Documented sandbox exception: MCP servers run unsandboxed (they are long-lived, user-configured and need their
    own files and network), but they never inherit the daemon's provider keys.
    """
    return child_env(extra)


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
        self.ended_at = 0.0  # monotonic time the last run ended
        self.connected_once = False  # a server that never connected is throttled from its failure, not its launch

    async def _call(self, session: Any, tool: str, args: dict[str, Any], fut: asyncio.Future[Any]) -> None:
        """One tool call. The caller may have been cancelled meanwhile (/stop, a timeout): its future is then done and
        resolving it raises InvalidStateError, which used to kill the runner for EVERY session."""
        try:
            res = await asyncio.wait_for(session.call_tool(tool, args), CALL_TIMEOUT)
            if not fut.done():
                fut.set_result(res)
        except asyncio.CancelledError:  # the runner shut down (reload, config change, transport error)
            if not fut.done():
                fut.set_exception(RuntimeError(f"mcp server {self.name} stopped during the call"))
            raise
        except Exception as e:  # noqa: BLE001
            if not fut.done():
                fut.set_exception(e)
            if _is_transport_error(e):
                self.state.status = "failed"  # the server is gone: stop advertising it as connected
                self.state.error = _describe(e)
                self.dead.set()

    async def _connect(self, stack: contextlib.AsyncExitStack) -> Any:
        """Open the transport and the session, initialise it and list the tools; returns the session."""
        from mcp import ClientSession

        if self.cfg.url:
            import httpx2  # the SDK's http library: a plain httpx client is rejected by its transport
            from mcp.client.streamable_http import streamable_http_client

            # read=300: a server may hold a response stream open (the SDK's own default); calls are bounded by
            # CALL_TIMEOUT in `call`
            http = httpx2.AsyncClient(
                headers=request_headers(self.cfg) or None, timeout=httpx2.Timeout(CONNECT_TIMEOUT, read=300.0)
            )
            await stack.enter_async_context(http)
            read, write = await stack.enter_async_context(streamable_http_client(self.cfg.url, http_client=http))
        else:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            if not self.cfg.command:
                raise ValueError("server needs `command` (stdio) or `url` (http)")
            params = StdioServerParameters(
                command=self.cfg.command,
                args=self.cfg.args,
                env=stdio_env(self.cfg.env),
                cwd=self.cfg.cwd,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
        self.state.tools = await asyncio.wait_for(_list_all_tools(session, self.name), CONNECT_TIMEOUT)
        return session

    async def _run(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                try:
                    session = await self._connect(stack)
                except BaseException as e:
                    if not isinstance(e, asyncio.CancelledError):
                        self.state.status = "failed"
                        self.state.error = _describe(e)
                    self.ended_at = time.monotonic()  # the restart throttle counts from here, not from the end of
                    self.ready.set()  # the teardown below; whoever waits for this server need not sit through it
                    raise
                self.state.status = "connected"
                self.connected_once = True
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
                        # a task cancelled before its first step never enters _call: resolve its future here too
                        call.add_done_callback(lambda _t, f=fut: _fail(f, f"mcp server {self.name} stopped"))
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
            self.ended_at = time.monotonic()
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


def warm_imports() -> None:
    """Import what a server connection needs (run in a thread: it takes ~0.6 s of CPU)."""
    with contextlib.suppress(Exception):  # a missing SDK is reported by the connection attempt, as before
        import mcp  # noqa: F401
        import mcp.client.stdio  # noqa: F401
        import mcp.client.streamable_http  # noqa: F401


MAX_TOOL_PAGES = 50


async def _list_all_tools(session: Any, server: str) -> list[McpToolInfo]:
    """Every tool of the server: ``tools/list`` is paginated (a server with many tools answers a page and a cursor), and
    two tools whose names sanitise to the same qualified name (``a.b`` / ``a_b``) cannot both be addressed: the first
    wins."""
    from mcp.types import PaginatedRequestParams

    tools: list[McpToolInfo] = []
    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(MAX_TOOL_PAGES):
        listed = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
        for t in listed.tools:
            info = McpToolInfo(server, t.name, t.description or "", dict(_attr(t, "input_schema", "inputSchema") or {}))
            if why := info.problem():
                logger.warning("mcp server %s: tool %r skipped (%s)", server, t.name, why)
                continue
            if info.qualified in seen:
                logger.warning("mcp server %s: tool %r collides with an earlier tool after renaming", server, t.name)
                continue
            seen.add(info.qualified)
            tools.append(info)
        cursor = _attr(listed, "next_cursor", "nextCursor")
        if not cursor:
            break
    return tools


def _content_text(res: Any) -> str:
    """The text of a tool result: text blocks, the text of embedded resources, a placeholder for what cannot be shown
    (image, audio, a resource link keeps its uri), and structured content when that is all the server sent."""
    parts: list[str] = []
    for c in res.content or []:
        kind = getattr(c, "type", "content")
        if getattr(c, "text", None):
            parts.append(c.text)
        elif kind == "resource" and getattr(getattr(c, "resource", None), "text", None):
            parts.append(c.resource.text)
        elif kind == "resource_link":
            parts.append(f"[resource: {getattr(c, 'uri', '')}]")
        else:
            parts.append(f"[{kind}]")
    text = "\n".join(parts)
    structured = _attr(res, "structured_content", "structuredContent")
    if structured and not any(getattr(c, "text", None) for c in res.content or []):
        text = (text + "\n" if text else "") + json.dumps(structured, ensure_ascii=False, default=str)
    return text


def _fail(fut: asyncio.Future[Any], msg: str) -> None:
    if not fut.done():
        fut.set_exception(RuntimeError(msg))


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
        self._last_tools: dict[str, list[McpToolInfo]] = {}
        self._started = False
        self._stale = False
        self._reaping: set[asyncio.Future[None]] = set()  # runners being stopped after a restart
        self._lock = asyncio.Lock()  # ensure_started runs on every turn of every session: single-flight

    def configure(self, servers: dict[str, McpServerConfig]) -> None:
        """Adopt new server definitions; running servers are restarted on the next ``ensure_started``."""
        if {k: v.model_dump() for k, v in servers.items()} != {k: v.model_dump() for k, v in self.servers.items()}:
            self._stale = True
        self.servers = dict(servers)

    @property
    def cold(self) -> bool:
        """Enabled servers are configured and not (or no longer, after a config change) running."""
        return any(c.enabled for c in self.servers.values()) and (not self._started or self._stale)

    async def start_early(self) -> None:
        """``ensure_started`` for a turn that has not reached its first model call yet: the SDK is imported off the
        event loop first (the import blocked it for ~0.6 s) so the user's hooks and the classifier running beside this
        are not slowed. The turn's own ``ensure_started`` then finds it done, or joins it."""
        await asyncio.to_thread(warm_imports)
        await self.ensure_started()

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._started and self._stale:
                await self._close()
            if self._started:
                await self._revive()
                return
            self._stale = False
            self._started = True
            self._launch_all()
            await self._await_ready()

    def _launch_all(self) -> None:
        for name, cfg in self.servers.items():
            self._runners[name] = self._launch(name, cfg)

    def _taken_by(self, name: str) -> str | None:
        """Another configured server whose tool prefix equals this one's after sanitising (``my.srv`` / ``my_srv`` both
        give ``mcp__my_srv__``): its tools would be reached through the first server's name."""
        mine = _SAFE.sub("_", name)
        for other in self.servers:
            if other == name:
                return None
            if _SAFE.sub("_", other) == mine and other in self._runners:
                return other
        return None

    def _launch(self, name: str, cfg: McpServerConfig) -> _Runner:
        runner = _Runner(name, cfg)
        if clash := self._taken_by(name):
            runner.state.status = "failed"
            runner.state.error = (
                f"name clashes with server {clash!r} (both give tools named mcp__{_SAFE.sub('_', name)}__*): rename one"
            )
            runner.ready.set()
            return runner
        if not cfg.enabled:
            runner.state.status = "disabled"
            runner.ready.set()
            return runner
        runner.started_at = time.monotonic()
        runner.start()
        return runner

    async def _await_ready(self, timeout: float | None = None) -> None:
        """Wait until every server has connected or failed. ``timeout`` (a revive) leaves a server that is still
        starting pending, to connect in the background; without it one that never answers is marked failed."""
        limit = CONNECT_TIMEOUT + 5 if timeout is None else timeout
        await asyncio.gather(
            *(asyncio.wait_for(r.ready.wait(), limit) for r in self._runners.values()), return_exceptions=True
        )
        if timeout is not None:
            return
        for r in self._runners.values():
            if r.state.status == "pending":
                r.state.status = "failed"
                r.state.error = "connect timeout"

    async def _revive(self) -> None:
        """Restart servers that failed to start or died, at most once per RESTART_BACKOFF_S each. ensure_started used
        to return as soon as it had run once, so a server that was down at daemon boot (network not up yet) or that
        crashed later stayed broken until `/mcp reload`."""
        now = time.monotonic()
        relaunched = False
        for name, r in list(self._runners.items()):
            gone = r.task is None or r.task.done() or r.dead.is_set() or r.state.status == "failed"
            # a server that never connected (down, or hanging until the connect timeout) is throttled from when it
            # failed: counted from its launch, one that took 30 s to fail was relaunched, and waited for, on every turn
            since = r.ended_at if not r.connected_once and r.ended_at else r.started_at
            if not r.cfg.enabled or not gone or now - since < RESTART_BACKOFF_S:
                continue
            logger.info("restarting mcp server %s (%s)", name, r.state.error or r.state.status)
            # the old runner may still be tearing its connection down (seconds for a hung stdio server): that goes on
            # beside the new one, the turn does not wait for it
            reap = asyncio.ensure_future(r.stop())
            self._reaping.add(reap)
            reap.add_done_callback(self._reaping.discard)
            self._runners[name] = self._launch(name, r.cfg)
            relaunched = True
        if relaunched:
            await self._await_ready(
                REVIVE_WAIT_S
            )  # a hung server must not hold the turn: it connects in the background

    async def reload(self, servers: dict[str, McpServerConfig] | None = None) -> None:
        await self.close()
        if servers is not None:
            self.servers = dict(servers)
        await self.ensure_started()

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        await asyncio.gather(*(r.stop() for r in self._runners.values()), *list(self._reaping), return_exceptions=True)
        self._runners.clear()
        self._last_tools.clear()
        self._started = False

    def states(self) -> list[McpServerState]:
        return [r.state for r in self._runners.values()]

    def tools(self) -> list[McpToolInfo]:
        """Every connected tool; a server that is restarting keeps the tools it had (a blip at a turn's start used to
        drop them from the tool list and the system prompt: a prompt-cache miss, twice, and the loaded schemas lost)."""
        out: list[McpToolInfo] = []
        for name, r in self._runners.items():
            if r.state.tools:
                self._last_tools[name] = r.state.tools
            if r.cfg.enabled:
                out += self._last_tools.get(name, [])
        return out

    def find(self, qualified: str) -> McpToolInfo | None:
        return next((t for t in self.tools() if qualified in (t.qualified, t.full_name)), None)

    async def call(self, qualified: str, args: dict[str, Any]) -> dict[str, Any]:
        info = self.find(qualified)
        if info is None:
            return {"error": f"Unknown MCP tool: {qualified}"}
        runner = self._runners[info.server]
        if runner.state.status != "connected" or runner.dead.is_set():
            # the server died after this turn started: revive it now (at most once per RESTART_BACKOFF_S) rather than
            # fail every call until the next turn's ensure_started
            await self.ensure_started()
            info = self.find(qualified)
            runner = self._runners.get(info.server) if info else None
            if info is None or runner is None or runner.state.status != "connected":
                status, why = (runner.state.status, runner.state.error) if runner else ("gone", "")
                return {"error": f"MCP server {info.server if info else qualified} is {status}: {why}"}
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        runner.queue.put_nowait((info.name, args, fut))
        try:
            res = await asyncio.wait_for(fut, CALL_TIMEOUT + 5)  # never hang a turn on a lost runner
        except TimeoutError:
            return {"error": f"MCP call timed out: {info.server} did not answer"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"MCP call failed: {_describe(e)}"}
        text = _content_text(res)
        return {"error": text or "MCP tool error"} if _attr(res, "is_error", "isError") else {"content": text}

    def search(self, query: str, limit: int = 8) -> list[McpToolInfo]:
        """Tools whose qualified name/description match ``query`` (exact names first). The notations the prompt itself
        prints work: ``select:a,b``, ``mcp__server__{a, b}``, a comma or space separated list of names. ``select:``
        and a full qualified name pick exactly those tools: ``get_issue`` does not bring in ``get_issue_comments``."""
        q = _expand_names(query)
        words = [w for w in re.split(r"[\s,]+", q) if w]
        tools = self.tools()
        qualified = {n.lower() for t in tools for n in (t.qualified, t.full_name)}
        # activating a tool sends its schema with every later request: a name asked for must not pull in others
        keywords = [] if query.strip().lower().startswith("select:") else [w for w in words if w not in qualified]
        scored = []
        for t in tools:
            hay = f"{t.qualified} {t.description}".lower()
            # the server's own name, and the sanitized one the prompt prints after the prefix (``search.docs``)
            names = {t.qualified.lower(), t.full_name.lower(), t.name.lower()}
            names.add(t.qualified[len(qualified_name(t.server, "")) :].lower())
            if q in names or names.intersection(words):
                score = 100
            else:
                score = sum(hay.count(w) for w in keywords)
            if score:
                scored.append((score, t))
        scored.sort(key=lambda p: (-p[0], p[1].qualified))
        return [t for _, t in scored[:limit]]
