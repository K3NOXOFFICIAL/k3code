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
import re
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

_request_counter = itertools.count(1)


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub_surrogates(value: str) -> str:
    """``value`` with each unpaired UTF-16 surrogate (a file name that was not UTF-8 reads as ``'caf\\udce9.txt'``) as
    U+FFFD. sqlite, stdout, sockets and every UTF-8 encoder refuse one: a single such character stopped all persistence
    of a session, and a frame that carried one was dropped on the way out."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return _LONE_SURROGATE.sub("\ufffd", value)
    return value


def _dumps(obj: Any) -> str:
    return scrub_surrogates(json.dumps(obj, ensure_ascii=False))


def next_request_id(prefix: str = "k3") -> str:
    """Unique id for server→client request frames."""
    return f"{prefix}-{next(_request_counter)}"


def encode_response(req_id: Any, result: Any) -> str:
    return _dumps({"jsonrpc": "2.0", "id": req_id, "result": result})


def encode_error(req_id: Any, code: int, message: str, data: Any = None) -> str:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return _dumps({"jsonrpc": "2.0", "id": req_id, "error": err})


def encode_event(event_type: str, payload: dict[str, Any] | None = None, importance: str | None = None) -> str:
    params: dict[str, Any] = {"type": event_type, "payload": payload or {}}
    if importance:
        params["importance"] = importance  # the TUI's focusPolicy reads it beside type/payload
    return _dumps({"jsonrpc": "2.0", "method": "event", "params": params})


def encode_server_request(method: str, params: dict[str, Any], req_id: str | None = None) -> str:
    return _dumps({"jsonrpc": "2.0", "id": req_id or next_request_id(), "method": method, "params": params})


def valid_id(value: Any) -> bool:
    """A JSON-RPC id: a string, a whole number or null (a bool is not a number here)."""
    return value is None or isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def reply_id(obj: Any) -> Any:
    """The id an error reply to ``obj`` carries: the request's own when it is a well-formed one, else null."""
    if isinstance(obj, dict) and valid_id(obj.get("id")):
        return obj.get("id")
    return None


def decode_frame(line: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse one newline-delimited frame. Returns (obj, error_message).

    A frame that is JSON and an object but not a valid request comes back as ``(obj, error)``, so the reply can carry
    its id (see :func:`reply_id`). The message starts with "parse error", "invalid request" or "invalid params" (the
    code the reply gets). Never raises: a 200 KB line of ``[`` is a RecursionError in the json module.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        return None, f"parse error: {e}"
    except RecursionError:
        return None, "parse error: nesting too deep"
    if not isinstance(obj, dict):
        return None, "invalid request: frame must be an object"
    if obj.get("jsonrpc") != "2.0":
        return obj, "invalid request: missing or bad jsonrpc version"
    if "id" in obj and not valid_id(obj["id"]):
        return obj, "invalid request: id must be a string, a whole number or null"
    if "method" in obj and not isinstance(obj["method"], str):
        return obj, "invalid request: method must be a string"
    if obj.get("params") is not None and not isinstance(obj["params"], dict):
        return obj, "invalid params: params must be an object"
    return obj, None
