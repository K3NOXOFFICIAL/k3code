"""Minimal HTTP listener for ``webhook`` triggers: ``POST /hook/<automation id>`` with the automation's token.

Binds ``127.0.0.1`` only, and only when ``automation.webhook_port`` is configured. The token is accepted as
``Authorization: Bearer <token>`` or ``X-K3-Token``. Wrong or missing token → 401, unknown automation → 404.

The answer says whether the run really started: ``202 {"ok": true}`` only when it did; a cooldown or rate limit →
``429`` with ``Retry-After`` (seconds), a previous run still going → ``409``, both with a JSON ``error`` and ``reason``.
The handler decides before the answer is written: it returns the run (an awaitable, started in the background) or a
:class:`Rejected`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("k3code.automation.webhook")

MAX_HEADER_BYTES = 16 * 1024
READ_TIMEOUT_S = 10.0  # reading + authenticating the request; the action itself is not bound by it
MAX_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Rejected:
    """A handler's answer when the run does not start: the HTTP ``status``, a short ``error`` code, a human ``reason``
    and, for 429, ``retry_after`` (whole seconds, sent as ``Retry-After``)."""

    status: int
    error: str
    reason: str
    retry_after: int | None = None


#: Called with the event while the request is still open; returns the run to start in the background, or a
#: :class:`Rejected` (an ``async def`` handler always accepts: calling it only creates the coroutine).
Handler = Callable[[dict[str, Any]], "Awaitable[Any] | Rejected"]

_REASONS = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    429: "Too Many Requests",
}


class WebhookServer:
    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.port = port
        self.host = host
        self._hooks: dict[str, tuple[str, Handler]] = {}
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()  # fired actions: they outlive the HTTP request that started them

    def register(self, automation_id: str, token: str, handler: Handler) -> None:
        self._hooks[automation_id] = (token, handler)

    def unregister(self, automation_id: str) -> None:
        self._hooks.pop(automation_id, None)

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # port 0 → the one the OS picked (tests)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, action: Awaitable[Any]) -> None:
        """Run the accepted action in the background and answer 202 now: a prompt/goal/shell action takes far longer
        than the request timeout (it used to be cancelled 10 s in and the sender saw 400, then retried)."""

        async def run() -> None:
            try:
                await action
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("webhook action failed")

        task = asyncio.get_running_loop().create_task(run(), name="k3-webhook-action")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body = await asyncio.wait_for(self._handle(reader), READ_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            status, body = 400, {"error": "bad request"}
        payload = json.dumps(body).encode()
        retry = f"Retry-After: {body['retry_after']}\r\n" if body.get("retry_after") is not None else ""
        writer.write(
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\n{retry}"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        with contextlib.suppress(Exception):
            await writer.drain()
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, Any]]:
        head = await reader.readuntil(b"\r\n\r\n")
        if len(head) > MAX_HEADER_BYTES:
            return 400, {"error": "headers too large"}
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            return 400, {"error": "bad request line"}
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)}
        if method != "POST":
            return 405, {"error": "POST only"}
        parts = path.split("?", 1)[0].strip("/").split("/")
        hook = self._hooks.get(parts[1]) if len(parts) == 2 and parts[0] == "hook" else None
        if hook is None:
            return 404, {"error": "no such automation"}
        token, handler = hook
        given = headers.get("x-k3-token") or headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given.encode(), token.encode()):
            return 401, {"error": "invalid token"}
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            return 413, {"error": "body too large"}
        raw = await reader.readexactly(length) if length else b""
        # Decide before answering: a 202 used to go out for every authenticated request while the run was dropped
        # afterwards (cooldown, rate limit, previous run still going) with only an INFO log, so the sender never knew.
        outcome = handler({"event": "webhook", "body": raw.decode("utf-8", "replace")[:2000]})
        if isinstance(outcome, Rejected):
            out: dict[str, Any] = {"ok": False, "error": outcome.error, "reason": outcome.reason}
            if outcome.retry_after is not None:
                out["retry_after"] = outcome.retry_after
            return outcome.status, out
        self._spawn(outcome)
        return 202, {"ok": True}
