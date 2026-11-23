"""The gateway's client for k3d, the Rust sidecar daemon (``rust/crates/k3d``).

k3d serves newline-delimited JSON-RPC on ``<K3CODE_HOME>/run/k3d.sock`` and, like the gateway, takes a method only
after ``gateway.auth`` with the token it wrote next to its socket (``k3d.token``). :func:`call` opens one connection
per request, authenticates with :func:`k3code.gateway.auth.authenticate` (the frame k3d expects), sends the method and
returns k3d's result. Any way k3d cannot answer (no socket, a stale one, no token, a refused token, a hang, a bad
reply) raises :class:`K3dUnavailable`; nothing here logs a frame, so the token never reaches a log line.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from k3code import paths
from k3code.gateway import auth as gw_auth
from k3code.gateway.protocol import MAX_FRAME_CHARS

#: The ``error.data.code`` of a k3d method the gateway could not forward.
UNAVAILABLE_CODE = "k3d_unavailable"
#: How long one forwarded request (connect, auth, reply) may take before k3d counts as unavailable.
TIMEOUT_S = 5.0
#: Request id of the forwarded call (a string no TUI request id uses).
REQUEST_ID = "k3-k3d-call"


class K3dUnavailable(Exception):
    """k3d did not answer the forwarded request; the message says why, never with the token."""


def socket_path() -> Path:
    """Where k3d listens by default (``k3d`` without ``--socket``)."""
    return paths.home() / "run" / "k3d.sock"


async def _exchange(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, sock: Path, method: str) -> Any:
    await gw_auth.authenticate(reader, writer, sock, timeout=TIMEOUT_S)
    req = {"jsonrpc": "2.0", "id": REQUEST_ID, "method": method, "params": {}}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    while line := await reader.readline():
        msg = json.loads(line)
        if isinstance(msg, dict) and msg.get("id") == REQUEST_ID and "method" not in msg:
            if "error" in msg:
                err = msg["error"]
                why = err.get("message", err) if isinstance(err, dict) else err
                raise K3dUnavailable(f"k3d refused {method}: {why}")
            return msg.get("result")
    raise K3dUnavailable(f"k3d closed the connection before answering {method}")


async def call(method: str, sock: Path | None = None) -> Any:
    """Forward ``method`` (no params) to k3d and return its result; raises :class:`K3dUnavailable`."""
    sock = sock or socket_path()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(sock), limit=MAX_FRAME_CHARS), TIMEOUT_S
        )
    except (OSError, TimeoutError) as e:
        raise K3dUnavailable(f"k3d is not running at {sock} ({type(e).__name__})") from None
    try:
        return await asyncio.wait_for(_exchange(reader, writer, sock, method), TIMEOUT_S)
    except K3dUnavailable:
        raise
    except gw_auth.AuthFailed as e:  # the messages name the token file, never the token
        raise K3dUnavailable(f"k3d refused the gateway's token: {e}") from None
    except TimeoutError:
        raise K3dUnavailable(f"k3d did not answer {method} within {TIMEOUT_S:g}s") from None
    except (OSError, ValueError) as e:  # a reset connection, an over-long or non-JSON line
        raise K3dUnavailable(f"k3d sent no usable reply to {method} ({type(e).__name__})") from None
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
