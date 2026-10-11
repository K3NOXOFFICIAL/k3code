"""A reply that used the whole output limit without writing, and one the context window cut short."""

from __future__ import annotations

import pytest

from k3code.agent.loop import MAX_WIDENED_TOKENS, AgentLoop
from k3code.errors import ContextOverflow
from k3code.providers.types import Message, StreamEvent, Usage
from k3code.router import Router, build_chain


class Recording:
    """Fake provider: one reply per call; records the messages and the output limit of every call."""

    def __init__(self, replies):
        self.replies, self.calls, self.limits = replies, [], []
        self.name, self.base_url = "fake", "https://fake.test"

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.calls.append(list(messages))
        self.limits.append(max_tokens)
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        yield StreamEvent(type="done", message=reply, usage=Usage())

    async def aclose(self):
        pass


def loop_for(provider, cwd, **kw) -> AgentLoop:
    router = Router(build_chain([provider], [["m"]]), max_retries=0)
    return AgentLoop(router, system_prompt="t", max_turns=10, permission_mode="yolo", cwd=cwd, **kw)


def reply(content, stop="end_turn"):
    return Message(role="assistant", content=content, stop_reason=stop)


async def test_an_empty_reply_that_used_the_whole_limit_is_retried_once_with_a_larger_one(tmp_path):
    p = Recording([reply(None, "max_tokens"), reply("the answer")])
    loop = loop_for(p, tmp_path)
    texts: list[str] = []

    async def collect(delta: str) -> None:
        texts.append(delta)

    loop.on_text_delta = collect
    events = [e async for e in loop.run("go", max_tokens=8192)]
    assert p.limits == [8192, 16384]
    # no "continue" nudge about an answer that was never written, and the empty reply is not history
    assert not any("cut off" in str(m.content) for m in p.calls[1])
    assert [m.role for m in p.calls[1]] == ["system", "user"]
    assert events[-1].message.content == "the answer"


async def test_a_second_empty_reply_ends_with_a_visible_notice_not_three_more_calls(tmp_path):
    p = Recording([reply(None, "max_tokens")])
    loop = loop_for(p, tmp_path)
    texts: list[str] = []

    async def collect(delta: str) -> None:
        texts.append(delta)

    loop.on_text_delta = collect
    events = [e async for e in loop.run("go", max_tokens=8192)]
    assert len(p.calls) == 2, "one wider retry, then stop"
    note = "".join(texts)
    assert "whole 16384-token output limit" in note and "max_tokens" in note
    assert events[-1].type == "done"


async def test_the_widened_limit_never_passes_the_bound(tmp_path):
    p = Recording([reply(None, "max_tokens")])
    [e async for e in loop_for(p, tmp_path).run("go", max_tokens=MAX_WIDENED_TOKENS)]
    assert p.limits == [MAX_WIDENED_TOKENS], "already at the bound: no retry, just the notice"


async def test_a_cut_off_reply_with_text_is_still_continued(tmp_path):
    p = Recording([reply("part one", "max_tokens"), reply(" part two")])
    [e async for e in loop_for(p, tmp_path).run("go")]
    assert len(p.calls) == 2 and p.limits == [8192, 8192]


async def test_the_output_limit_never_exceeds_a_quarter_of_a_small_window(tmp_path):
    p = Recording([reply("hi")])
    [e async for e in loop_for(p, tmp_path, context_window=8192).run("go", max_tokens=8192)]
    assert p.limits == [2048]
    p = Recording([reply("hi")])
    [e async for e in loop_for(p, tmp_path, context_window=200_000).run("go", max_tokens=8192)]
    assert p.limits == [8192]


async def test_a_reply_cut_by_the_context_window_raises_overflow_instead_of_ending_the_turn(tmp_path):
    p = Recording([reply("half an ans", "model_context_window_exceeded")])
    loop = loop_for(p, tmp_path)
    resets: list[int] = []

    async def on_reset() -> None:
        resets.append(1)

    loop.on_text_reset = on_reset
    with pytest.raises(ContextOverflow):
        [e async for e in loop.run("go")]
    assert resets == [1], "what was streamed of the half reply is retracted"
