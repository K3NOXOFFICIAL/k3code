"""Agent loop integration tests with fake provider."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from k3code.agent.loop import AgentLoop
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.router import Router, build_chain


class FakeProvider:
    """A provider that returns pre-programmed stream events per call."""

    def __init__(self, turn_events: list[list[StreamEvent]]):
        self._turn_events = turn_events
        self._call_count = 0
        self.name = "fake"
        self.base_url = "https://fake.test"

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        events = self._turn_events[self._call_count] if self._call_count < len(self._turn_events) else []
        self._call_count += 1
        for event in events:
            yield event

    async def aclose(self):
        pass


def make_tool_call_event(tool_call: ToolCall) -> StreamEvent:
    return StreamEvent(type="tool_call", tool_call=tool_call)


def make_text_event(text: str) -> StreamEvent:
    return StreamEvent(type="text_delta", text=text)


def make_done_event(message: Message) -> StreamEvent:
    return StreamEvent(type="done", message=message, usage=Usage())


@pytest.fixture
def temp_cwd():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.mark.asyncio
async def test_agent_loop_write_then_read(temp_cwd):
    """Agent calls write tool, then read tool, then answers."""
    test_file = temp_cwd / "test.txt"
    write_call = ToolCall(id="call_1", name="write", arguments={"path": str(test_file), "content": "hi"})
    read_call = ToolCall(id="call_2", name="read", arguments={"path": str(test_file)})

    # First turn: model requests write
    msg1 = Message(role="assistant", content=None, tool_calls=[write_call])
    # Second turn: model requests read
    msg2 = Message(role="assistant", content=None, tool_calls=[read_call])
    # Third turn: model answers
    msg3 = Message(role="assistant", content="The file contains 'hi'", tool_calls=[])

    provider = FakeProvider([
        [make_tool_call_event(write_call), make_done_event(msg1)],
        [make_tool_call_event(read_call), make_done_event(msg2)],
        [make_text_event("The file contains 'hi'"), make_done_event(msg3)],
    ])

    chain = build_chain([provider], [["fake-model"]])
    router = Router(chain, max_retries=0)

    system_prompt = "You are a test agent."
    loop = AgentLoop(router, system_prompt=system_prompt, max_turns=10, permission_mode="yolo", cwd=temp_cwd)

    events = []
    async for event in loop.run(f"Create {test_file} with 'hi' and read it back"):
        events.append(event)

    # Check final message was yielded
    # Now yields: turn1 assistant done, tool result, turn2 assistant done, tool result, turn3 assistant done = 5
    done_events = [e for e in events if e.type == "done"]
    assert len(done_events) == 5
    final_msg = done_events[-1].message
    assert final_msg is not None
    assert final_msg.role == "assistant"
    assert "hi" in final_msg.content


@pytest.mark.asyncio
async def test_agent_loop_tool_calls_only_on_final_message(temp_cwd):
    """Regression test: real providers (openai_compat, anthropic) only attach
    parsed tool calls to the final "done" message — they never emit a separate
    "tool_call"-type StreamEvent as the call is assembled off the wire. The loop
    must still execute the tool in that case (it must not rely solely on
    "tool_call" events, which no real provider currently produces).
    """
    test_file = temp_cwd / "test.txt"
    write_call = ToolCall(id="call_1", name="write", arguments={"path": str(test_file), "content": "hi"})
    msg1 = Message(role="assistant", content=None, tool_calls=[write_call])
    msg2 = Message(role="assistant", content="Wrote the file", tool_calls=[])

    # Note: no make_tool_call_event(...) here, matching the real providers' contract.
    provider = FakeProvider([
        [make_done_event(msg1)],
        [make_done_event(msg2)],
    ])

    chain = build_chain([provider], [["fake-model"]])
    router = Router(chain, max_retries=0)

    loop = AgentLoop(router, system_prompt="test", max_turns=5, permission_mode="yolo", cwd=temp_cwd)

    events = []
    async for event in loop.run(f"Create {test_file} with 'hi'"):
        events.append(event)

    assert test_file.read_text() == "hi"
    tool_results = [e for e in events if e.type == "done" and e.message and e.message.role == "tool"]
    assert len(tool_results) == 1
    assert "True" in tool_results[0].message.content  # str(result) of {"ok": True, "path": ...}


@pytest.mark.asyncio
async def test_agent_loop_max_turns(temp_cwd):
    """Agent stops after max_turns."""
    # Always returns a tool call, never finishes
    test_file = temp_cwd / "x.txt"
    tool_call = ToolCall(id="call_1", name="write", arguments={"path": str(test_file), "content": "x"})
    msg = Message(role="assistant", content=None, tool_calls=[tool_call])

    provider = FakeProvider([
        [make_tool_call_event(tool_call), make_done_event(msg)],
    ] * 10)  # Repeat enough times

    chain = build_chain([provider], [["fake-model"]])
    router = Router(chain, max_retries=0)

    loop = AgentLoop(router, system_prompt="test", max_turns=3, permission_mode="yolo", cwd=temp_cwd)

    events = []
    async for event in loop.run("Do something"):
        events.append(event)

    # Should have exactly 3 turns * 2 (assistant done + tool result) = 6
    done_events = [e for e in events if e.type == "done"]
    assert len(done_events) == 6


@pytest.mark.asyncio
async def test_agent_permission_denied_headless(temp_cwd):
    """In headless ask mode, side-effect tools are denied."""
    test_file = temp_cwd / "x.txt"
    write_call = ToolCall(id="call_1", name="write", arguments={"path": str(test_file), "content": "x"})
    msg = Message(role="assistant", content=None, tool_calls=[write_call])
    # Second turn: model gets the error and responds
    msg2 = Message(role="assistant", content="Permission denied, cannot write", tool_calls=[])

    provider = FakeProvider([
        [make_tool_call_event(write_call), make_done_event(msg)],
        [make_text_event("Permission denied, cannot write"), make_done_event(msg2)],
    ])

    chain = build_chain([provider], [["fake-model"]])
    router = Router(chain, max_retries=0)

    loop = AgentLoop(router, system_prompt="test", max_turns=5, permission_mode="ask", cwd=temp_cwd)

    events = []
    async for event in loop.run("Write a file"):
        events.append(event)

    # Should get a tool result with error
    tool_results = [e for e in events if e.type == "done" and e.message and e.message.role == "tool"]
    assert len(tool_results) == 1
    assert "Permission denied" in tool_results[0].message.content


@pytest.mark.asyncio
async def test_identical_replies_in_separate_turns_are_not_a_doom_loop(temp_cwd):
    """The loop guard judges repetition inside one run. A session whose /loop tick answers 'nothing
    changed' every time shares one Reliability across turns; that must not count as repeating itself
    (it escalated 40% of the soak's cheap background turns to the main tier)."""
    from k3code.reliability import Reliability

    msg = Message(role="assistant", content="Nothing changed.", tool_calls=[])
    provider = FakeProvider([[make_done_event(msg)]] * 12)
    router = Router(build_chain([provider], [["fake-model"]]), max_retries=0)
    reliability = Reliability.from_settings(None, session="s")

    for _ in range(12):
        loop = AgentLoop(
            router, system_prompt="t", max_turns=5, permission_mode="yolo", cwd=temp_cwd, reliability=reliability
        )
        async for _event in loop.run("check"):
            pass
        assert loop.escalation_reason is None
        assert reliability.needs_input is False


@pytest.mark.asyncio
async def test_a_long_session_of_turns_never_hits_the_recursion_limit(temp_cwd):
    """One AgentLoop per turn on a shared router and bundle, past 1000 turns (the 72 h soak died at ~490)."""
    from k3code.reliability import Reliability

    msg = Message(role="assistant", content="ok", tool_calls=[])
    provider = FakeProvider([[make_done_event(msg)]] * 1300)
    router = Router(build_chain([provider], [["fake-model"]]), max_retries=0, on_event=lambda e: None)
    reliability = Reliability.from_settings(None, session="s")

    for i in range(1300):
        loop = AgentLoop(
            router, system_prompt="t", max_turns=3, permission_mode="yolo", cwd=temp_cwd, reliability=reliability
        )
        async for _event in loop.run(f"turn {i}"):
            pass
    assert provider._call_count == 1300


@pytest.mark.asyncio
async def test_a_stop_does_not_poison_the_next_turn_of_the_session(temp_cwd):
    """/stop sets the bundle's cancel token; the next turn of the same session must run normally."""
    from k3code.reliability import Reliability

    msg = Message(role="assistant", content="hello", tool_calls=[])
    provider = FakeProvider([[make_done_event(msg)]] * 3)
    router = Router(build_chain([provider], [["fake-model"]]), max_retries=0)
    reliability = Reliability.from_settings(None, session="s")
    reliability.cancel()  # what the gateway does on /stop
    loop = AgentLoop(
        router, system_prompt="t", max_turns=3, permission_mode="yolo", cwd=temp_cwd, reliability=reliability
    )
    done = [e async for e in loop.run("again") if e.type == "done"]
    assert done and done[-1].message.content == "hello"
    assert provider._call_count == 1
    assert reliability.cancel_token.cancelled is False


class _FlakyMidStream:
    """Streams half an answer, dies, then streams the whole answer (a proxy that dropped the connection)."""

    name = "flaky"
    base_url = "https://flaky.test"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        import httpx

        self.calls += 1
        yield make_text_event("Hello ")
        if self.calls == 1:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        yield make_text_event("world")
        yield make_done_event(Message(role="assistant", content="Hello world", tool_calls=[]))

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_a_retry_after_partial_output_resets_the_consumers_copy(temp_cwd):
    """The router used to retry silently after text had already been yielded: consumers saw "Hello Hello world"."""
    provider = _FlakyMidStream()
    router = Router(build_chain([provider], [["fake-model"]]), max_retries=1, base_delay=0.0, max_delay=0.0)
    loop = AgentLoop(router, system_prompt="t", max_turns=3, permission_mode="yolo", cwd=temp_cwd)
    seen: list[str] = []
    resets: list[int] = []

    async def on_delta(chunk: str) -> None:
        seen.append(chunk)

    async def on_reset() -> None:
        resets.append(len(seen))
        seen.clear()

    loop.on_text_delta = on_delta
    loop.on_text_reset = on_reset
    events = [e async for e in loop.run("hi")]
    assert provider.calls == 2
    assert resets == [1]  # one partial chunk was discarded
    assert "".join(seen) == "Hello world"
    assert [e.type for e in events].count("reset") == 1
    done = [e for e in events if e.type == "done"]
    assert done[-1].message.content == "Hello world"
