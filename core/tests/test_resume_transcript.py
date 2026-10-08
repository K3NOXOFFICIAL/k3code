"""Transcript persistence + crash-resume decisions on the Reliability bundle."""

from __future__ import annotations

from pathlib import Path

from k3code.providers.types import Message, ToolCall
from k3code.reliability.hooks import Reliability


def test_transcript_roundtrip(tmp_path: Path) -> None:
    r = Reliability.from_settings(None, session="s1", home=tmp_path)
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "ls"})]),
    ]
    r.save_transcript(msgs)
    loaded = Reliability.from_settings(None, session="s1", home=tmp_path).load_transcript()
    assert loaded is not None
    assert loaded[1].tool_calls[0].arguments == {"command": "ls"}


def test_interrupted_only_for_side_effect_intent_without_done(tmp_path: Path) -> None:
    r = Reliability.from_settings(None, session="s2", home=tmp_path)
    bash = ToolCall(id="b1", name="bash", arguments={"command": "x"})
    read = ToolCall(id="r1", name="read_file", arguments={"path": "f"})
    r.journal_intent(bash, side_effect=True)
    r.journal_intent(read, side_effect=False)
    r2 = Reliability.from_settings(None, session="s2", home=tmp_path)
    res = r2.interrupted_for(bash)
    assert res is not None and "INTERRUPTED" in res["error"]
    assert r2.interrupted_for(read) is None  # pure tools are simply re-run
    r.journal_done("b1", {"content": "ok"})
    assert Reliability.from_settings(None, session="s2", home=tmp_path).interrupted_for(bash) is None
