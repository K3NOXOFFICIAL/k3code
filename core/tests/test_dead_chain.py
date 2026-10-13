"""A chain whose entries all fail with 5xx or hang is not a reset window to park on, and a call under a deadline still
reaches its second entry.

The router skips a primary that stayed down through every retry for FAILOVER_COOLDOWN_S, so the next calls go to the
free entry. When every entry is in that window there is nothing to skip toward: the walk tries them again, and
``earliest_reset`` reports no wait (a server window is not a provider-declared reset).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest

from k3code.errors import ChainExhausted
from k3code.providers.base import Provider, ProviderError, to_provider_error
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.stall import StreamStalled
from k3code.providers.types import Message, StreamEvent
from k3code.reliability.events import PARKED, EventEmitter
from k3code.reliability.persistent_retry import PersistentRetry, RetryConfig
from k3code.router import CooldownStore, Router, build_chain
from k3code.router.classifier import FailoverReason
from k3code.routing.caller import ModelCaller
from k3code.routing.tiers import TaskKind, Tier

MSGS = [Message(role="user", content="hi")]
URL = "https://x.test"


def server_error() -> ProviderError:
    return ProviderError("service unavailable", status_code=503)


def stalled() -> ProviderError:
    return to_provider_error(StreamStalled("stream stalled: no content from the model within 60s"), kind="openai")


class Entry(Provider):
    """Fails every call with ``fail()`` while it is set, otherwise answers ``reply``."""

    base_url = URL
    api_key = "k"

    def __init__(self, name: str, *, fail=None, reply: str = "ok") -> None:
        self.name = name
        self.fail = fail
        self.reply = reply
        self.calls = 0

    async def stream(self, messages, tools, model, **kwargs) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail()
        yield StreamEvent(type="done", message=Message(role="assistant", content=self.reply))

    async def aclose(self) -> None:
        pass


async def no_sleep(_seconds: float) -> None:
    pass


class Clock:
    """The park's clock: a sleep moves it, nothing waits. (The cooldown store keeps the real one, so a 60 s window
    never ends inside a test: the strict case.)"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def router_of(entries, store: CooldownStore | None = None) -> Router:
    return Router(build_chain(entries, [["m"]] * len(entries)), sleep=no_sleep, cooldowns=store or CooldownStore())


def rig(entries, *, unattended: bool = False):
    clock, events, parks = Clock(), EventEmitter(), []
    events.add(lambda e: parks.append(e) if e.kind == PARKED else None)
    router = router_of(entries)
    # max_wait keeps a regression a failed assertion: parked forever, a fake clock never runs out of waiting
    config = RetryConfig(max_wait=7200)
    retry = PersistentRetry(router, None, config=config, events=events, sleep=clock.sleep, monotonic=clock.monotonic)
    retry.unattended = unattended
    return retry, router, parks


async def drain(stream):
    return [e async for e in stream]


# failure, the reason the chain reports, and the attempts one entry gets per call (a stall is retried once)
DEAD = [pytest.param(server_error, "server", 3, id="503"), pytest.param(stalled, "timeout", 2, id="stall")]


@pytest.mark.parametrize(("fail", "reason", "tries"), DEAD)
async def test_a_chain_of_dead_entries_fails_an_attended_turn_at_once(fail, reason, tries):
    a, b = Entry("a", fail=fail), Entry("b", fail=fail)
    retry, _, parks = rig([a, b])
    for _ in range(2):  # the second call finds both entries in their window
        with pytest.raises(ChainExhausted) as err:
            await drain(retry.stream(MSGS, []))
        assert err.value.last_reason == reason
        assert err.value.retry_after is None, "a 60 s skip window is not a reset to wait for"
    assert parks == []
    assert (a.calls, b.calls) == (2 * tries, 2 * tries), "every call tries both entries again"


@pytest.mark.parametrize(("fail", "reason", "tries"), DEAD)
async def test_a_chain_of_dead_entries_ends_an_unattended_run_after_the_park_cap(fail, reason, tries):
    a, b = Entry("a", fail=fail), Entry("b", fail=fail)
    retry, _, parks = rig([a, b], unattended=True)
    with pytest.raises(ChainExhausted) as err:
        await drain(retry.stream(MSGS, []))
    cap = retry.config.max_unattended_parks
    assert len(parks) == cap == 6
    assert err.value.last_reason == reason
    assert a.calls == b.calls == (cap + 1) * tries


async def test_a_dead_first_entry_is_still_skipped_on_the_next_call_while_the_second_is_free():
    a, b = Entry("a", fail=server_error), Entry("b")
    router = router_of([a, b])
    assert (await router.complete(MSGS, [])).content == "ok"
    assert a.calls == 3 and b.calls == 1
    assert (await router.complete(MSGS, [])).content == "ok"
    assert a.calls == 3, "the dead primary sat out its window"
    assert b.calls == 2


async def test_a_later_entrys_window_is_checked_when_the_walk_reaches_it():
    store = CooldownStore()
    store.arm(FailoverReason.server, provider="b", model="m", base_url=URL, fixed_seconds=60)
    a, b = Entry("a", fail=server_error), Entry("b")
    router = router_of([a, b], store)
    # b looked free-or-not only while a was still untried; a has failed since, and b is all that is left
    assert (await router.complete(MSGS, [])).content == "ok"
    assert b.calls == 1


async def test_an_earlier_entry_that_failed_without_a_window_is_not_a_free_one():
    store = CooldownStore()
    store.arm(FailoverReason.server, provider="b", model="m", base_url=URL, fixed_seconds=60)
    a = Entry("a", fail=lambda: ProviderError("unknown model", status_code=400))  # fails over, arms no window
    b = Entry("b")
    assert (await router_of([a, b], store).complete(MSGS, [])).content == "ok"
    assert (a.calls, b.calls) == (1, 1)


async def test_an_entry_tried_through_its_window_that_answers_is_out_of_it():
    store = CooldownStore()
    a, b = Entry("a", fail=server_error), Entry("b", fail=server_error)
    router = router_of([a, b], store)
    with pytest.raises(ChainExhausted):
        await router.complete(MSGS, [])
    a.fail = None  # a recovers while still in its window; b is still down
    assert (await router.complete(MSGS, [])).content == "ok"
    assert a.calls == 4
    assert not store.in_cooldown(provider="a", model="m", base_url=URL), "a stale window would skip the live entry"
    assert store.in_cooldown(provider="b", model="m", base_url=URL)


def test_earliest_reset_does_not_wait_on_a_server_or_timeout_window():
    store = CooldownStore()
    router = router_of([Entry("a"), Entry("b")], store)
    store.arm(FailoverReason.server, provider="a", model="m", base_url=URL, fixed_seconds=60)
    store.arm(FailoverReason.timeout, provider="b", model="m", base_url=URL, fixed_seconds=60)
    assert router.earliest_reset() is None
    # a rate limit is a reset to wait for, unless another entry is free to try (a server window counts as free)
    store.arm(FailoverReason.rate_limit, provider="a", model="m", base_url=URL, retry_after=3600)
    assert router.earliest_reset() is None
    store.arm(FailoverReason.rate_limit, provider="b", model="m", base_url=URL, retry_after=1800)
    assert 1700 < router.earliest_reset() <= 1800


# ── a call under a deadline ────────────────────────────────────────────


def caller_of(router: Router) -> ModelCaller:
    return ModelCaller(lambda: {t: router for t in Tier}, SimpleNamespace(task_tiers=None, providers=[]), None)


async def test_a_call_with_a_deadline_fails_over_on_the_first_stall():
    a, b = Entry("a", fail=stalled), Entry("b", reply="summary")
    caller = caller_of(router_of([a, b]))
    res = await caller.complete(TaskKind.COMPACTION, MSGS, timeout=90)
    assert res.text == "summary"
    assert a.calls == 1, "a second try on a would only be cut off by the deadline"
    await caller.complete(TaskKind.COMPACTION, MSGS, timeout=90)
    assert (a.calls, b.calls) == (1, 2), "the stalled entry sits out its window"


async def test_a_call_without_a_deadline_keeps_the_stall_retry():
    a, b = Entry("a", fail=stalled), Entry("b", reply="summary")
    await caller_of(router_of([a, b])).complete(TaskKind.TITLE, MSGS)
    assert a.calls == 2


class Hang(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(3600)
        yield b""


async def test_a_summary_whose_first_entry_hangs_is_answered_by_the_second_inside_its_deadline():
    hits = {"A": 0, "B": 0}
    answer = (
        b'data: {"choices":[{"delta":{"content":"summary"},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
    )

    def client(name: str) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            hits[name] += 1
            body = {"stream": Hang()} if name == "A" else {"content": answer}
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, **body)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # the stall limit (0.4 s) twice is longer than the deadline (0.7 s): a retry on A would be cut off before B
    limits = {"first_event_timeout": 0.4, "idle_timeout": 0.4}
    a = OpenAICompatProvider(name="A", base_url="https://a.test/v1", api_key="k", client=client("A"), **limits)
    b = OpenAICompatProvider(name="B", base_url="https://b.test/v1", api_key="k", client=client("B"), **limits)
    caller = caller_of(router_of([a, b]))
    res = await caller.complete(TaskKind.COMPACTION, MSGS, timeout=0.7)
    assert res.text == "summary" and hits == {"A": 1, "B": 1}
    hits.update(A=0, B=0)
    await caller.complete(TaskKind.COMPACTION, MSGS, timeout=0.7)
    assert hits == {"A": 0, "B": 1}, "the hung entry was cooled down, not left to hang the next call too"
