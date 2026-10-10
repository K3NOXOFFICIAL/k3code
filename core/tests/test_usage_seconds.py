"""`call` and `tool` usage rows carry their wall time in ``seconds`` (``k3code stats`` showed 0 for every row)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.providers.fake import FakeProvider
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.router import RouterEvent


def _rows(db) -> list[dict]:
    cur = db._db.execute("SELECT kind, detail, provider, seconds FROM events ORDER BY ts")
    return [dict(zip(("kind", "detail", "provider", "seconds"), r, strict=True)) for r in cur]


async def test_gateway_turn_rows_carry_call_and_tool_seconds(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    script = tmp_path / "s.json"
    script.write_text(
        json.dumps(
            [
                {
                    "type": "tool_call",
                    "name": "bash",
                    "arguments": {"command": "sleep 0.2"},
                    "when": "turn_first",
                },
                {"type": "text", "text": "done", "when": "turn_after_tool"},
            ]
        )
    )
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    real_stream = FakeProvider.stream

    async def slow_stream(self, *a, **k):
        await asyncio.sleep(0.1)  # the model "thinks" before answering
        async for event in real_stream(self, *a, **k):
            yield event

    monkeypatch.setattr(FakeProvider, "stream", slow_stream)
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    srv = GatewayServer(
        config=Settings(providers=[prov], default_model="default", permission_mode="yolo"),
        store=SessionStore(tmp_path / "s.db"),
    )
    srv._write = lambda s: None
    project = tmp_path / "proj"
    project.mkdir()
    live = srv.live_for(srv.store.create(title="t", model="default", cwd=str(project)))
    status, text = await srv._run_turn(live, "run it")
    assert status == "done", (text, live.last_error)
    rows = _rows(srv.usage)
    calls = [r for r in rows if r["kind"] == "call"]
    tools = [r for r in rows if r["kind"] == "tool"]
    assert len(calls) == 2 and all(0.08 <= r["seconds"] < 5 for r in calls), calls
    assert len(tools) == 1 and tools[0]["detail"] == "bash" and 0.15 <= tools[0]["seconds"] < 5, tools


async def test_headless_usage_rows_carry_call_and_tool_seconds(tmp_path: Path, monkeypatch):
    from k3code.cli import _HeadlessUsage

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    usage = _HeadlessUsage("headless")
    call = ToolCall(id="c1", name="bash", arguments={"command": "x"})
    usage.on_router_event(RouterEvent(kind="router.attempt", provider="p", model="m", attempt=1))
    await asyncio.sleep(0.06)
    msg = Message(role="assistant", tool_calls=[call], usage=Usage(prompt_tokens=1, completion_tokens=1))
    usage.on_stream_event(StreamEvent(type="done", message=msg))
    await asyncio.sleep(0.06)
    result = Message(role="tool", content="ok", tool_call_id="c1", name="bash")
    usage.on_stream_event(StreamEvent(type="done", message=result))
    rows = _rows(usage.db)
    usage.close()
    (call_row,) = [r for r in rows if r["kind"] == "call"]
    (tool_row,) = [r for r in rows if r["kind"] == "tool"]
    assert 0.05 <= call_row["seconds"] < 5 and 0.05 <= tool_row["seconds"] < 5
    assert (tool_row["detail"], call_row["provider"]) == ("bash", "p")


async def test_side_call_row_carries_seconds(tmp_path: Path):
    from k3code.routing.caller import ModelCaller
    from k3code.usage import UsageDB

    class SlowRouter:
        async def complete(self, messages, tools, *, max_tokens):
            await asyncio.sleep(0.06)
            return Message(role="assistant", content="hi", usage=Usage(prompt_tokens=1, completion_tokens=1))

    class Routers:
        def get(self, tier):
            return SlowRouter()

    db = UsageDB(tmp_path / "usage.db")
    caller = ModelCaller(lambda: Routers(), Settings(providers=[]), db)
    await caller.complete("title", [Message(role="user", content="x")], session_id="s")
    (row,) = [r for r in _rows(db) if r["kind"] == "call"]
    db.close()
    assert 0.05 <= row["seconds"] < 5
