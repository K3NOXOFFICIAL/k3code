"""Who may drive the daemon: a per-daemon token in the private run directory.

The socket is 0600, but every unsandboxed child of the daemon (an MCP stdio server, an approved bash command) runs as
the same user and could connect to it. Privileged methods (``shell.exec``, ``config.set``, mode switches, slash
commands, ...) therefore need a connection that first sent ``gateway.auth`` with the token the daemon wrote at start.
The TUI's attach bridge and the CLI clients read it from ``<run dir>/gateway.token``; children never get the socket's
location through their environment (:data:`k3code.paths.GATEWAY_ENV_VARS`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

from k3code import paths
from k3code.paths import ensure_private_dir

#: Request id of the auth frame the bundled clients send first (a string no TUI request id uses).
AUTH_REQUEST_ID = "k3-gateway-auth"
AUTH_METHOD = "gateway.auth"


def token_path(sock: Path) -> Path:
    """The token file of the daemon serving ``sock``: next to it (``gateway.sock`` -> ``gateway.token``)."""
    return Path(sock).with_suffix(".token")


def lock_path(sock: Path) -> Path:
    """The single-instance lock of the daemon serving ``sock``: next to it, named after it."""
    return Path(sock).with_suffix(".lock")


def prepare_socket_dir(sock: Path) -> Path:
    """Make ``sock``'s directory safe for the socket and the token.

    The default ``<K3CODE_HOME>/run`` is ours: it is created or tightened to 0700. A directory the user chose through
    ``K3CODE_GATEWAY_SOCKET`` is never chmod'ed (it may be ``$HOME`` or ``/tmp``): it is created 0700 when missing and
    refused when other users could replace the socket or the token in it (group/other-writable without the sticky bit).
    """
    parent = Path(sock).parent
    if parent == paths.home() / "run":
        return ensure_private_dir(parent)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = parent.stat().st_mode
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise RuntimeError(
            f"{parent} is writable by other users; put the gateway socket in a directory only you can write to"
        )
    return parent


def write_token(sock: Path) -> str:
    """A fresh random token, written 0600 next to ``sock`` (replacing a stale one); returns it."""
    path = token_path(sock)
    token = secrets.token_hex(32)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()  # never write through an existing file or link
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    return token


def remove_token(sock: Path, token: str) -> None:
    """Remove the token file if it still holds ``token`` (a newer daemon's file stays)."""
    path = token_path(sock)
    with contextlib.suppress(OSError):
        if path.read_text().strip() == token:
            path.unlink()


def read_token(sock: Path) -> str | None:
    """The token of the daemon serving ``sock``, or None when it cannot be read."""
    try:
        return token_path(sock).read_text().strip() or None
    except OSError:
        return None


def auth_frame(sock: Path) -> bytes | None:
    """The ``gateway.auth`` request line a client sends first, or None without a readable token."""
    token = read_token(sock)
    if token is None:
        return None
    req = {"jsonrpc": "2.0", "id": AUTH_REQUEST_ID, "method": AUTH_METHOD, "params": {"token": token}}
    return (json.dumps(req) + "\n").encode()


class AuthFailed(ConnectionError):
    """The daemon refused (or never answered) this client's ``gateway.auth``."""


async def authenticate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    sock: Path,
    forward: Callable[[bytes], None] | None = None,
    timeout: float = 10.0,
) -> None:
    """Send ``gateway.auth`` on a fresh connection and wait for its reply; raises :class:`AuthFailed`.

    Lines the daemon sends before the reply (``gateway.ready``, the attach snapshot, notifications) go to
    ``forward`` (dropped without one), so a bridge can still hand them to its TUI.
    """
    frame = auth_frame(sock)
    if frame is None:
        raise AuthFailed(f"cannot read the daemon token {token_path(sock)}")
    writer.write(frame)
    await writer.drain()

    async def wait_reply() -> dict:
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if isinstance(msg, dict) and msg.get("id") == AUTH_REQUEST_ID and "method" not in msg:
                return msg
            if forward is not None:
                forward(line)
        raise AuthFailed("the daemon closed the connection during gateway.auth")

    try:
        reply = await asyncio.wait_for(wait_reply(), timeout)
    except TimeoutError:
        raise AuthFailed("the daemon did not answer gateway.auth") from None
    if "error" in reply:
        err = reply["error"]
        raise AuthFailed(str(err.get("message", err) if isinstance(err, dict) else err))
