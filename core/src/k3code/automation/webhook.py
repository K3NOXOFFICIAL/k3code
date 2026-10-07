"""Minimal HTTP listener for ``webhook`` triggers: ``POST /hook/<automation id>`` with the automation's token.

Binds ``127.0.0.1`` only, and only when ``automation.webhook_port`` is configured. The token is accepted as
``Authorization: Bearer <token>`` or ``X-K3-Token``. Wrong or missing token → 401, unknown automation → 404.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("k3code.automation.webhook")

MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 1024 * 1024
Handler = Callable[[dict[str, Any]], Awaitable[None]]

_REASONS = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
}


class WebhookServer:
    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.port = port
        self.host = host
        self._hooks: dict[str, tuple[str, Handler]] = {}
        self._server: asyncio.AbstractServer | None = None

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

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body = await asyncio.wait_for(self._handle(reader), 10)
        except Exception:  # noqa: BLE001
            status, body = 400, {"error": "bad request"}
        payload = json.dumps(body).encode()
        writer.write(
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
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
        await handler({"event": "webhook", "body": raw.decode("utf-8", "replace")[:2000]})
        return 202, {"ok": True}
