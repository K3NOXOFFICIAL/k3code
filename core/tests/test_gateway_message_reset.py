"""A model call that fails after streaming part of its reply is retried (and failed over) from the start.

The loop forgets the failed attempt's text, and the gateway has to say so to the client: it had sent the partial text as
``message.delta`` for every attempt, so the TUI showed "PARTIAL PARTIAL PARTIAL FULL" while the turn ran and the answer
twice once it completed. The gateway now sends ``message.reset`` for each discarded attempt that had streamed text.

The chain is ``pa`` (model m-a) then ``pb`` (m-b), both the scripted fake provider; the script's ``model`` filters give
each its own behaviour. The router's backoff is zeroed so the retries do not sleep.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from test_autonomy_gateway import events, k3home, run_turn, start

NO_GATE = {"plan_first": False, "proposals": False}  # no classifier, plan or proposal call: only the turn's own calls

PARTIAL = "PARTIAL-FROM-A "
FULL = "FULL-FROM-B"
DOWN = {"type": "error", "model": "m-a", "status_code": 503, "message": "service unavailable"}


def chain(tmp: Path, monkeypatch: pytest.MonkeyPatch, steps: list[dict], **cfg: Any) -> GatewayServer:
    monkeypatch.setenv("K3CODE_HOME", str(k3home(tmp)))
    script = tmp / "script.json"
    script.write_text(json.dumps(steps))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    monkeypatch.setattr("k3code.router.router.jittered_backoff", lambda *a, **k: 0.0)
    providers = [
        ProviderEntry(
            name=name, kind="openai", base_url=f"http://{name}", api_key_env="NOPE", models={"default": model}
        )
        for name, model in (("pa", "m-a"), ("pb", "m-b"))
    ]
    server = GatewayServer(
        config=Settings(providers=providers, permission_mode="yolo", autonomy=NO_GATE, **cfg),
        store=SessionStore(tmp / "sessions.db"),
    )
    frames: list[str] = []
    server._write = frames.append  # type: ignore[method-assign]
    server._frames = frames  # type: ignore[attr-defined]
    return server


def stream_of(server: GatewayServer) -> list[tuple[str, str]]:
    """The reply stream a client sees, in order: ('delta', text) and ('reset', '')."""
    out = []
    for line in server._frames:  # type: ignore[attr-defined]
        frame = json.loads(line)
        if frame.get("method") != "event":
            continue
        kind, payload = frame["params"]["type"], frame["params"]["payload"]
        if kind == "message.delta":
            out.append(("delta", payload["text"]))
        elif kind == "message.reset":
            out.append(("reset", ""))
    return out


def shown(stream: list[tuple[str, str]]) -> str:
    """What a client that appends deltas and drops its buffer on a reset is left with."""
    text = ""
    for kind, chunk in stream:
        text = text + chunk if kind == "delta" else ""
    return text


async def test_each_discarded_attempt_sends_a_reset_and_the_transcript_is_unchanged(tmp_path, monkeypatch):
    steps = [
        {"type": "text", "model": "m-a", "text": PARTIAL},
        DOWN,  # pa streams its partial text, then fails: the first attempt and both retries
        {"type": "text", "model": "m-b", "text": FULL},
    ]
    server = chain(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "say something")

    # the partial text went out three times (first attempt + 2 retries) and every copy was taken back
    assert stream_of(server) == [("delta", PARTIAL), ("reset", "")] * 3 + [("delta", FULL)]
    assert len(events(server, "message.reset")) == 3 and events(server, "message.reset")[0] == {}
    assert shown(stream_of(server)) == FULL  # a client following the stream ends with the answer once
    # ... and the stored transcript, the final text and the model calls are what they were
    assert [m["content"] for m in server.session.stored.messages if m["role"] == "assistant"] == [FULL]
    done = events(server, "message.complete")[-1]
    assert done["text"] == FULL and done["status"] == "done"
    assert [p.name for p in server.providers if p.log] == ["pa", "pb"]
    assert len([c for p in server.providers if p.name == "pa" for c in p.log]) == 3


async def test_no_reset_when_the_failed_attempt_streamed_no_text(tmp_path, monkeypatch):
    server = chain(tmp_path, monkeypatch, [DOWN, {"type": "text", "model": "m-b", "text": FULL}])
    await start(server, tmp_path)
    await run_turn(server, "say something")

    assert events(server, "message.reset") == []  # nothing had been sent for the client to discard
    assert stream_of(server) == [("delta", FULL)]
    assert server.session.stored.messages[-1]["content"] == FULL


async def test_a_clean_turn_sends_no_reset(tmp_path, monkeypatch):
    server = chain(tmp_path, monkeypatch, [{"type": "text", "model": "m-a", "text": "all fine"}])
    await start(server, tmp_path)
    await run_turn(server, "say something")

    assert events(server, "message.reset") == []
    assert stream_of(server) == [("delta", "all fine")]


async def test_a_reset_only_concerns_the_call_that_streamed_text(tmp_path, monkeypatch):
    """Step 1 is answered in full ("Looking." and a tool call). Step 2 on pa streams only a tool call and then fails:
    the router still resets that attempt, but there is no text of it on the client, and the client holds step 1's
    text (already complete) that a reset must not be allowed to touch."""
    steps = [
        {"type": "text", "model": "m-a", "when": "first", "text": "Looking."},
        {
            "type": "tool_call",
            "model": "m-a",
            "when": "first",
            "id": "c1",
            "name": "bash",
            "arguments": {"command": "echo hi"},
        },
        {
            "type": "tool_call",
            "model": "m-a",
            "when": "after_tool",
            "id": "c2",
            "name": "bash",
            "arguments": {"command": "echo again"},
        },
        {**DOWN, "when": "after_tool"},
        {"type": "text", "model": "m-b", "when": "after_tool", "text": FULL},
    ]
    server = chain(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "look around")

    assert stream_of(server) == [("delta", "Looking."), ("delta", FULL)]
    assert events(server, "message.reset") == []
    assert [m["content"] for m in server.session.stored.messages if m["role"] == "assistant"][-1] == FULL


async def test_partial_text_after_a_tool_is_reset_and_the_text_before_it_is_not_repeated(tmp_path, monkeypatch):
    steps = [
        {"type": "text", "model": "m-a", "when": "first", "text": "Looking."},
        {
            "type": "tool_call",
            "model": "m-a",
            "when": "first",
            "id": "c1",
            "name": "bash",
            "arguments": {"command": "echo hi"},
        },
        {"type": "text", "model": "m-a", "when": "after_tool", "text": PARTIAL},
        {**DOWN, "when": "after_tool"},
        {"type": "text", "model": "m-b", "when": "after_tool", "text": FULL},
    ]
    server = chain(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "look around")

    stream = stream_of(server)
    assert stream == [("delta", "Looking.")] + [("delta", PARTIAL), ("reset", "")] * 3 + [("delta", FULL)]
    assert len(events(server, "message.reset")) == 3
    # the tool ran once, between the first text and the discarded attempts
    assert len(events(server, "tool.complete")) == 1
