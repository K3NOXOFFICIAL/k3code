"""JSON-RPC 2.0 framing helpers for the stdio gateway transport.

Wire shape (matches the TUI's json-rpc-channel):
- client→server request:  {"jsonrpc":"2.0","id":<n|s>,"method":<str>,"params":{...}}
- server→client response: {"jsonrpc":"2.0","id":<n|s>,"result":{...}}
  or error:               {"jsonrpc":"2.0","id":<n|s>,"error":{"code":<int>,"message":<str>}}
- server→client event:    {"jsonrpc":"2.0","method":"event","params":{"type":<str>,"payload":{...}}}
- server→client request:  {"jsonrpc":"2.0","id":<str>,"method":<str>,"params":{...,"session_id":...}}
  (approval / clarify / sudo / secret; the client answers with a result frame)
- client notification (no id): no response is sent.

Standard error codes: -32700 parse error, -32600 invalid request,
-32601 method not found, -32602 invalid params, -32603 internal error.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: Server-defined: the method needs a connection that sent ``gateway.auth`` with the daemon token.
UNAUTHORIZED = -32001
#: Server-defined: this connection already has the maximum of concurrent long-running requests in flight.
TOO_MANY_REQUESTS = -32002

#: A frame longer than this is shrunk before it is sent: the TUI builds one JS string per line, V8 caps a string at
#: ~512 MB, and an event that size (a huge tool result) made the TUI throw on every event that followed.
MAX_FRAME_CHARS = 8 * 1024 * 1024
#: Strings longer than this are cut (head and tail kept) when a frame is over MAX_FRAME_CHARS.
_SHRUNK_STRING_CHARS = 64 * 1024

_request_counter = itertools.count(1)


def next_request_id(prefix: str = "k3") -> str:
    """Unique id for server→client request frames."""
    return f"{prefix}-{next(_request_counter)}"


def _shrink(value: Any, limit: int) -> Any:
    """``value`` with every string longer than ``limit`` cut to its head and tail around a marker."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        half = limit // 2
        cut = len(value) - 2 * half
        return f"{value[:half]}\n[... {cut} characters cut: too large for one frame ...]\n{value[-half:]}"
    if isinstance(value, dict):
        return {k: _shrink(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v, limit) for v in value]
    return value


def _dump_capped(frame: dict[str, Any], payload_key: str, parent: str | None = None) -> str:
    """The frame as one JSON line; over MAX_FRAME_CHARS its long strings are cut so the line fits (the shape stays)."""
    line = json.dumps(frame, ensure_ascii=False)
    if len(line) <= MAX_FRAME_CHARS:
        return line
    target = frame[parent] if parent else frame
    for limit in (_SHRUNK_STRING_CHARS, 4096, 256):
        target[payload_key] = _shrink(target[payload_key], limit)
        line = json.dumps(frame, ensure_ascii=False)
        if len(line) <= MAX_FRAME_CHARS:
            return line
    target[payload_key] = {"error": "frame too large to send"}
    return json.dumps(frame, ensure_ascii=False)


def encode_response(req_id: Any, result: Any) -> str:
    return _dump_capped({"jsonrpc": "2.0", "id": req_id, "result": result}, "result")


def encode_error(req_id: Any, code: int, message: str, data: Any = None) -> str:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": err}, ensure_ascii=False)


def encode_event(event_type: str, payload: dict[str, Any] | None = None, importance: str | None = None) -> str:
    params: dict[str, Any] = {"type": event_type, "payload": payload or {}}
    if importance:
        params["importance"] = importance  # the TUI's focusPolicy reads it beside type/payload
    return _dump_capped({"jsonrpc": "2.0", "method": "event", "params": params}, "payload", parent="params")


def encode_server_request(method: str, params: dict[str, Any], req_id: str | None = None) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": req_id or next_request_id(), "method": method, "params": params},
        ensure_ascii=False,
    )


def decode_frame(line: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse one newline-delimited frame. Returns (obj, error_message)."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        return None, f"parse error: {e}"
    if not isinstance(obj, dict):
        return None, "invalid request: frame must be an object"
    if obj.get("jsonrpc") != "2.0":
        return None, "invalid request: missing or bad jsonrpc version"
    return obj, None
