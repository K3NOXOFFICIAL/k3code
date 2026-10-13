"""A side call books the prompt of every attempt that died after the provider counted it (``partial_usage``), as the
agent loop does for a turn. The final message's usage covers only the attempt that finished."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from k3code.errors import ChainExhausted
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.base import Provider, ProviderError
from k3code.providers.types import Message, StreamEvent, Usage
from k3code.router import Router, build_chain
from k3code.routing.caller import ModelCaller, attempt_holder
from k3code.routing.tiers import TaskKind, Tier

MSGS = [Message(role="user", content="summarise")]

START = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":150000}}}\n\n'
TEXT = (
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"summary"}}\n\n'
)
DELTA = (
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":7}}\n\n'
)
STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class Books:
    """What the session was charged and what the usage log got."""

    def __init__(self) -> None:
        self.charged: list[tuple[int, int]] = []
        self.rows: list[dict] = []
        self.db = SimpleNamespace(record=lambda kind, **kw: self.rows.append({"kind": kind, **kw}))

    def charge(self, session_id: str, usage: Usage) -> None:
        self.charged.append((usage.prompt_tokens, usage.completion_tokens))

    @property
    def logged(self) -> list[tuple[int, int]]:
        return [(r["tokens_in"], r["tokens_out"]) for r in self.rows]


def caller_of(router: Router, books: Books) -> ModelCaller:
    return ModelCaller(
        lambda: {t: router for t in Tier},
        SimpleNamespace(task_tiers=None, providers=[]),
        books.db,
        charge=books.charge,
    )


async def no_sleep(_seconds: float) -> None:
    pass


class Body(httpx.AsyncByteStream):
    def __init__(self, *, hang: bool) -> None:
        self.hang = hang

    async def __aiter__(self):
        yield START
        if self.hang:
            await asyncio.sleep(3600)
        yield TEXT
        yield DELTA
        yield STOP


async def test_the_prompt_of_a_stalled_attempt_is_booked_next_to_the_retry_that_answered():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body(hang=len(sent) == 1))

    provider = AnthropicProvider(
        name="a",
        base_url="https://a.test",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        first_event_timeout=0.3,
        idle_timeout=0.3,
    )
    books = Books()
    router = Router(build_chain([provider], [["claude-sonnet-4-5"]]), sleep=no_sleep)
    res = await caller_of(router, books).complete(TaskKind.COMPACTION, MSGS, session_id="s1")
    assert res.text == "summary" and len(sent) == 2
    assert books.charged == [(150000, 0), (150000, 7)], "both requests were counted by the provider"
    assert books.logged == [(150000, 0), (150000, 7)]
    assert [r["model"] for r in books.rows] == ["claude-sonnet-4-5"] * 2
    assert books.rows[0]["detail"] and not books.rows[1]["detail"], "the dead attempt's row says what it is"


class Counting(Provider):
    """Counts its prompt up front (``partial_usage``), then dies, hangs, or answers."""

    base_url = "https://c.test"
    api_key = "k"

    def __init__(self, name: str, *, prompt: int = 700, then: str = "die") -> None:
        self.name = name
        self.prompt = prompt
        self.then = then
        self.calls = 0

    async def stream(self, messages, tools, model, **kwargs):
        self.calls += 1
        yield StreamEvent(type="partial_usage", usage=Usage(prompt_tokens=self.prompt))
        if self.then == "die":
            raise ProviderError("service unavailable", status_code=503)
        if self.then == "hang":
            await asyncio.sleep(3600)
        final = Usage(prompt_tokens=self.prompt, completion_tokens=5)
        yield StreamEvent(type="done", message=Message(role="assistant", content="ok", usage=final), usage=final)

    async def aclose(self) -> None:
        pass


class Quiet(Provider):
    """Counts nothing up front: only its final message carries usage."""

    base_url = "https://q.test"
    api_key = "k"

    def __init__(self, name: str) -> None:
        self.name = name

    async def stream(self, messages, tools, model, **kwargs):
        final = Usage(prompt_tokens=40, completion_tokens=2)
        yield StreamEvent(type="done", message=Message(role="assistant", content="ok", usage=final), usage=final)

    async def aclose(self) -> None:
        pass


async def test_a_dead_attempt_is_booked_when_the_failover_provider_counts_nothing_up_front():
    books = Books()
    router = Router(build_chain([Counting("a"), Quiet("b")], [["m"], ["m"]]), max_retries=0, sleep=no_sleep)
    await caller_of(router, books).complete(TaskKind.TITLE, MSGS, session_id="s1")
    assert books.charged == [(700, 0), (40, 2)]


async def test_an_attempt_that_finishes_is_booked_once_with_its_final_usage():
    books = Books()
    router = Router(build_chain([Counting("a", then="answer")], [["m"]]), max_retries=0, sleep=no_sleep)
    await caller_of(router, books).complete(TaskKind.TITLE, MSGS, session_id="s1")
    assert books.charged == [(700, 5)]
    assert books.logged == [(700, 5)]


async def test_the_prompts_of_a_chain_that_died_are_booked_though_the_call_fails():
    books = Books()
    a, b = Counting("a", prompt=700), Counting("b", prompt=900)
    router = Router(build_chain([a, b], [["m"], ["m"]]), max_retries=0, sleep=no_sleep)
    with pytest.raises(ChainExhausted):
        await caller_of(router, books).complete(TaskKind.TITLE, MSGS, session_id="s1", escalate=False)
    assert books.charged == [(700, 0), (900, 0)]


async def test_the_prompt_of_an_attempt_the_deadline_cut_off_is_booked():
    books = Books()
    router = Router(build_chain([Counting("a", then="hang")], [["m"]]), max_retries=0, sleep=no_sleep)
    with pytest.raises(TimeoutError):
        await caller_of(router, books).complete(TaskKind.COMPACTION, MSGS, session_id="s1", timeout=0.2)
    assert books.charged == [(700, 0)]


async def test_each_dead_attempt_is_booked_to_the_entry_it_was_sent_to():
    books = Books()
    seen: list[tuple[str, str]] = []

    def on_event(event) -> None:  # what the gateway does: the call in flight collects the attempts
        if event.kind == "router.attempt" and (held := attempt_holder.get()) is not None:
            held.append((event.provider, event.model))
            seen.append((event.provider, event.model))

    chain = build_chain([Counting("a"), Counting("b", then="answer")], [["m1"], ["m2"]])
    router = Router(chain, max_retries=0, sleep=no_sleep, on_event=on_event)
    await caller_of(router, books).complete(TaskKind.TITLE, MSGS, session_id="s1")
    assert [(r["provider"], r["model"]) for r in books.rows] == [("a", "m1"), ("b", "m2")]
    assert seen == [("a", "m1"), ("b", "m2")]


async def test_a_prompt_count_with_no_reset_after_it_is_booked_when_the_next_count_arrives():
    class Streaming:  # a router that does not tell the consumer to reset (the loop's _metered covers this case too)
        async def stream(self, messages, tools, **kwargs):
            yield StreamEvent(type="partial_usage", usage=Usage(prompt_tokens=100))
            yield StreamEvent(type="partial_usage", usage=Usage(prompt_tokens=200))
            final = Usage(prompt_tokens=200, completion_tokens=3)
            yield StreamEvent(type="done", message=Message(role="assistant", content="ok", usage=final), usage=final)

    books = Books()
    await caller_of(Streaming(), books).complete(TaskKind.TITLE, MSGS, session_id="s1")
    assert books.charged == [(100, 0), (200, 3)]
