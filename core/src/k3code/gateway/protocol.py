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

_request_counter = itertools.count(1)


def next_request_id(prefix: str = "k3") -> str:
    """Unique id for server→client request frames."""
    return f"{prefix}-{next(_request_counter)}"


def encode_response(req_id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)


def encode_error(req_id: Any, code: int, message: str, data: Any = None) -> str:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": err}, ensure_ascii=False)


def encode_event(event_type: str, payload: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "method": "event", "params": {"type": event_type, "payload": payload or {}}},
        ensure_ascii=False,
    )


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
