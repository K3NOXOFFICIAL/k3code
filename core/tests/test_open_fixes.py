"""Output-limit cut-offs, per-run error state and /stop during streaming."""

from __future__ import annotations

import httpx

from k3code.agent.loop import CUT_OFF_CONTINUE, AgentLoop
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.router import Router, build_chain

MSGS = [Message(role="user", content="hi")]


def _client(body: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _done(provider) -> Message:
    events = [e async for e in provider.stream(MSGS, [], "m")]
    assert events[-1].type == "done"
    return events[-1].message


async def test_anthropic_reports_max_tokens_stop_reason():
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Hi"}}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
        '"usage":{"output_tokens":1}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    p = AnthropicProvider(name="a", base_url="https://a.test", api_key="k", client=_client(body))
    assert (await _done(p)).stop_reason == "max_tokens"


async def test_openai_length_finish_maps_to_max_tokens():
    body = (
        'data: {"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n'
        "data: [DONE]\n\n"
    )
    p = OpenAICompatProvider(name="o", base_url="https://o.test/v1", api_key="k", client=_client(body))
    assert (await _done(p)).stop_reason == "max_tokens"


class Scripted:
    """Fake provider: one list of events per call; records the messages of every call."""

    def __init__(self, turns, on_event=None):
        self.turns, self.calls, self.on_event, self.yielded = turns, [], on_event, 0
        self.name, self.base_url = "fake", "https://fake.test"

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.calls.append(list(messages))
        turn = self.turns[len(self.calls) - 1] if len(self.calls) <= len(self.turns) else []
        for event in turn:
            self.yielded += 1
            yield event
            if self.on_event:
                self.on_event(self.yielded)

    async def aclose(self):
        pass


def _loop(provider, cwd) -> AgentLoop:
    router = Router(build_chain([provider], [["m"]]), max_retries=0)
    return AgentLoop(router, system_prompt="t", max_turns=10, permission_mode="yolo", cwd=cwd)


def _done_ev(msg: Message) -> StreamEvent:
    return StreamEvent(type="done", message=msg, usage=Usage())


def _text(m: Message) -> str:
    c = m.content
    return c if isinstance(c, str) else str(c)


async def test_text_cut_off_at_max_tokens_is_continued(tmp_path):
    cut = Message(role="assistant", content="part one", stop_reason="max_tokens")
    rest = Message(role="assistant", content=" part two")
    p = Scripted([[_done_ev(cut)], [_done_ev(rest)]])
    [e async for e in _loop(p, tmp_path).run("go")]
    assert len(p.calls) == 2, "a cut-off answer must not end the run"
    assert any(m.role == "user" and _text(m) == CUT_OFF_CONTINUE for m in p.calls[1])


async def test_continuations_are_capped(tmp_path):
    cut = Message(role="assistant", content="x", stop_reason="max_tokens")
    p = Scripted([[_done_ev(cut)]] * 10)
    [e async for e in _loop(p, tmp_path).run("go")]
    assert len(p.calls) == 4  # the answer plus at most 3 continue nudges


async def test_tool_call_cut_off_mid_arguments_is_not_executed(tmp_path):
    target = tmp_path / "half.txt"
    call = ToolCall(id="c1", name="write", arguments={"_unparsed": '{"path": "' + str(target) + '", "cont'})
    cut = Message(role="assistant", content=None, tool_calls=[call], stop_reason="max_tokens")
    p = Scripted([[_done_ev(cut)], [_done_ev(Message(role="assistant", content="ok"))]])
    [e async for e in _loop(p, tmp_path).run("go")]
    assert not target.exists()
    results = [m for m in p.calls[1] if m.role == "tool"]
    assert results and "output token limit" in _text(results[-1])


async def test_tool_error_state_resets_between_runs(tmp_path):
    p = Scripted([[_done_ev(Message(role="assistant", content="done"))]])
    loop = _loop(p, tmp_path)
    loop._tool_errors, loop._failed = 99, ["stale"]
    [e async for e in loop.run("go")]
    assert loop._tool_errors == 0 and loop._failed == []


async def test_stop_ends_a_reply_that_is_still_streaming(tmp_path):
    holder = {}

    def stop_after_first(n):
        if n == 1:
            holder["loop"].interrupt()

    turn = [StreamEvent(type="text_delta", text=f"t{i}") for i in range(50)]
    turn.append(_done_ev(Message(role="assistant", content="never")))
    p = Scripted([turn], on_event=stop_after_first)
    holder["loop"] = loop = _loop(p, tmp_path)
    events = [e async for e in loop.run("go")]
    assert p.yielded <= 3, f"kept reading the stream after /stop ({p.yielded} events)"
    assert sum(1 for e in events if e.type == "text_delta") <= 2
