#!/usr/bin/env python3
"""Deterministic OpenAI-compatible upstream (streaming chat completions) for chaos runs.

Used when the real OmniRoute is unavailable (e.g. its API key hit its daily quota).
Stateless script: the first user message holds ``RUN[<shell command>]`` markers; turn N
issues the Nth bash tool call, and once all ran it answers with a final text.
"""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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

    def do_POST(self) -> None:
        req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
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
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
