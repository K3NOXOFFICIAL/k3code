"""Write protocol_frames.jsonl: frame lines the Python gateway encodes or decodes, for tests/golden.rs.

Run from the repository root:  core/.venv/bin/python rust/crates/k3d/tests/fixtures/gen_protocol_frames.py

Each record holds a ``line`` as ``gateway/protocol.py`` (or ``gateway/auth.py``) produced it and ``python_ok``, what
``protocol.decode_frame`` says about it. Encoder records also name the encoder and its arguments, so Rust can encode
the same frame and compare. ``rust_differs`` marks a line the two decoders judge differently on purpose (Rust's
decoder is stricter); the test pins that difference rather than skipping it.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from k3code.gateway import auth, protocol

OUT = Path(__file__).with_name("protocol_frames.jsonl")
FAKE_TOKEN = "not-a-real-token"


def encoded(name: str, encoder: str, args: dict, line: str) -> dict:
    return {"name": name, "encoder": encoder, "args": args, "line": line}


def records() -> list[dict]:
    out = [
        encoded("response", "response", {"id": 3, "result": {"ok": True, "s": "ü"}},
                protocol.encode_response(3, {"ok": True, "s": "ü"})),
        encoded("response with a string id", "response", {"id": "k3-9", "result": [1, None, "x\ny"]},
                protocol.encode_response("k3-9", [1, None, "x\ny"])),
        encoded("error with a null id", "error", {"id": None, "code": -32700, "message": "parse error", "data": None},
                protocol.encode_error(None, -32700, "parse error")),
        encoded("error with data", "error", {"id": 4, "code": -32601, "message": "no such method", "data": {"m": "x"}},
                protocol.encode_error(4, -32601, "no such method", {"m": "x"})),
        encoded("unauthorized error", "error",
                {"id": 5, "code": protocol.UNAUTHORIZED, "message": "wrong or missing gateway token", "data": None},
                protocol.encode_error(5, protocol.UNAUTHORIZED, "wrong or missing gateway token")),
        encoded("event", "event", {"type": "message.delta", "payload": {"text": "hi"}, "importance": "high"},
                protocol.encode_event("message.delta", {"text": "hi"}, "high")),
        encoded("event without payload or importance", "event", {"type": "x", "payload": None, "importance": ""},
                protocol.encode_event("x", None, "")),
        encoded("server request", "server_request", {"id": "k3-1", "method": "approval", "params": {"session_id": "s"}},
                protocol.encode_server_request("approval", {"session_id": "s"}, "k3-1")),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        sock = Path(tmp) / "k3d.sock"
        auth.token_path(sock).write_text(FAKE_TOKEN)
        frame = auth.auth_frame(sock)
        assert frame is not None
        out.append(encoded("auth frame", "auth_frame", {"token": FAKE_TOKEN}, frame.decode()))
    for name, line, rust_differs in [
        ("notification", '{"jsonrpc":"2.0","method":"k3d.ping"}', None),
        ("request", '{"jsonrpc":"2.0","id":1,"method":"k3d.ping","params":{}}', None),
        ("not json", "not json", None),
        ("an array", "[1,2]", None),
        ("no jsonrpc member", '{"id":1,"method":"x"}', None),
        ("jsonrpc 1.0", '{"jsonrpc":"1.0","id":1,"method":"x"}', None),
        ("a null request id", '{"jsonrpc":"2.0","id":null,"method":"x"}', "a request id must not be null"),
        ("a float request id", '{"jsonrpc":"2.0","id":1.5,"method":"x"}', "an id must be an integer or a string"),
        ("a non-string method", '{"jsonrpc":"2.0","id":1,"method":5}', "a method must be a string"),
        ("no method, result or error", '{"jsonrpc":"2.0","id":1}', "a frame must be a request, response or error"),
    ]:
        record = {"name": name, "line": line}
        if rust_differs:
            record["rust_differs"] = rust_differs
        out.append(record)
    for record in out:
        record["python_ok"] = protocol.decode_frame(record["line"])[1] is None
    return out


def main() -> int:
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
