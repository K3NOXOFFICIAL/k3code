"""A gateway frame is capped: an oversized event is shrunk, never sent whole (#81)."""

from __future__ import annotations

import json

from k3code.gateway import protocol


def test_small_frames_are_untouched() -> None:
    event = json.loads(protocol.encode_event("message.delta", {"text": "hi"}, importance="low"))
    assert event == {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": "message.delta", "payload": {"text": "hi"}, "importance": "low"},
    }
    assert json.loads(protocol.encode_response(7, {"ok": True})) == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


def test_an_oversized_event_is_shrunk_and_stays_valid_json(monkeypatch) -> None:
    monkeypatch.setattr(protocol, "MAX_FRAME_CHARS", 200_000)
    big = "A" * 50_000 + "MIDDLE" + "Z" * 5_000_000
    line = protocol.encode_event("tool.done", {"name": "bash", "result": {"stdout": big, "code": 0}})
    assert len(line) <= 200_000
    event = json.loads(line)
    assert event["method"] == "event" and event["params"]["type"] == "tool.done"
    result = event["params"]["payload"]["result"]
    assert result["code"] == 0 and event["params"]["payload"]["name"] == "bash"
    assert result["stdout"].startswith("A" * 1000) and result["stdout"].endswith("Z" * 1000)
    assert "too large for one frame" in result["stdout"] and "MIDDLE" not in result["stdout"]


def test_an_oversized_response_is_shrunk_too(monkeypatch) -> None:
    monkeypatch.setattr(protocol, "MAX_FRAME_CHARS", 100_000)
    line = protocol.encode_response("r1", {"messages": [{"content": "x" * 3_000_000} for _ in range(3)]})
    assert len(line) <= 100_000
    decoded = json.loads(line)
    assert decoded["id"] == "r1" and len(decoded["result"]["messages"]) == 3


def test_many_small_strings_fall_back_to_a_placeholder(monkeypatch) -> None:
    monkeypatch.setattr(protocol, "MAX_FRAME_CHARS", 1_000)
    line = protocol.encode_event("blob", {"items": ["y" * 100 for _ in range(500)]})
    assert len(line) <= 1_000
    assert json.loads(line)["params"]["payload"] == {"error": "frame too large to send"}
