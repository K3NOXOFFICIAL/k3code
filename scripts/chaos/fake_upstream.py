#!/usr/bin/env python3
"""Deterministic OpenAI-compatible upstream (streaming chat completions) for chaos runs.

Used when the real OmniRoute is unavailable (e.g. its API key hit its daily quota).
Stateless script: the first user message holds ``RUN[<shell command>]`` markers; turn N
issues the Nth bash tool call, and once all ran it answers with a final text.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


#: Chaos knobs (set from the command line in __main__):
#:   --good-key K     only ``Authorization: Bearer K`` is accepted, anything else gets 401
#:   --ratelimit S    the first chat request opens a rate-limit window: it and every request during the next S
#:                    seconds get ``429`` with ``Retry-After: <remaining seconds>``; afterwards requests succeed
#:   --log FILE       append one line per chat request: ``<ts> <status> <key-prefix>``
OPTS: dict = {"good_key": None, "ratelimit": 0.0, "log": None, "window_end": None}
LOCK = threading.Lock()


def sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        body = json.dumps({"data": [{"id": "fake"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, code: int, headers: dict[str, str], body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _log(self, status: int) -> None:
        if OPTS["log"]:
            key = (self.headers.get("authorization") or "").removeprefix("Bearer ")[:4]
            with LOCK, open(OPTS["log"], "a") as f:
                f.write(f"{time.time():.3f} {status} {key}\n")

    def do_POST(self) -> None:
        req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        good = OPTS["good_key"]
        if good is not None and self.headers.get("authorization") != f"Bearer {good}":
            self._log(401)
            return self._reject(401, {}, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}})
        if OPTS["ratelimit"]:
            with LOCK:
                now = time.monotonic()
                if OPTS["window_end"] is None:
                    OPTS["window_end"] = now + OPTS["ratelimit"]
                left = OPTS["window_end"] - now
            if left > 0:
                self._log(429)
                return self._reject(429, {"Retry-After": str(max(1, round(left)))},
                                    {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}})
        self._log(200)
        msgs = req.get("messages", [])
        first = next((m["content"] for m in msgs if m["role"] == "user"), "") or ""
        cmds = re.findall(r"RUN\[(.*?)\]", first, re.S)
        tools = [m for m in msgs if m["role"] == "tool"]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        usage = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
        if len(tools) < len(cmds):
            call = {"index": 0, "id": f"call_{len(tools)}", "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": cmds[len(tools)]})}}
            self.wfile.write(sse({"choices": [{"delta": {"tool_calls": [call]}}]}))
            self.wfile.write(sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": usage}))
        else:
            last = (tools[-1]["content"] if tools else "")[:200].replace("\n", " ")
            self.wfile.write(sse({"choices": [{"delta": {"content": f"DONE. last tool result: {last}"}}]}))
            self.wfile.write(sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}))
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int)
    ap.add_argument("--good-key")
    ap.add_argument("--ratelimit", type=float, default=0.0)
    ap.add_argument("--log")
    a = ap.parse_args()
    OPTS.update(good_key=a.good_key, ratelimit=a.ratelimit, log=a.log)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
