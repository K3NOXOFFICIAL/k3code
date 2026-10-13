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


# ── an entry skipped for a "free" later entry is tried after all when that one turns out dead ───────────────────────


def _arm_window(router: Router, index: int) -> None:
    e = router.chain[index]
    router.cooldowns.arm(
        FailoverReason.server, provider=e.provider_name, model=e.model, base_url=e.base_url, fixed_seconds=60.0
    )


async def test_a_skipped_dead_entry_is_tried_when_the_free_one_after_it_is_dead_too():
    """A recovered while its 60 s window ran; B looked free, so A was skipped, B failed: the walk ended with a healthy
    entry never tried and no wait to park on (an attended turn died, an unattended one gave up its parks)."""
    a, b = Entry("a", reply="from a"), Entry("b", fail=server_error)
    router = router_of([a, b])
    _arm_window(router, 0)
    events = await drain(router.stream(MSGS, [], max_tokens=10, temperature=None))
    assert events[-1].message.content == "from a"
    assert (a.calls, b.calls) == (1, 3)


async def test_two_skipped_dead_entries_are_both_tried_when_the_free_one_after_them_is_dead():
    a, b, c = Entry("a", fail=server_error), Entry("b", reply="from b"), Entry("c", fail=server_error)
    router = router_of([a, b, c])
    _arm_window(router, 0)
    _arm_window(router, 1)
    events = await drain(router.stream(MSGS, [], max_tokens=10, temperature=None))
    assert events[-1].message.content == "from b"
    assert (a.calls, b.calls, c.calls) == (3, 1, 3)  # c fails first; then a (dead too) and b, in chain order


async def test_a_skipped_dead_entry_stays_skipped_when_the_free_one_after_it_answers():
    a, b = Entry("a", reply="from a"), Entry("b", reply="from b")
    router = router_of([a, b])
    _arm_window(router, 0)
    events = await drain(router.stream(MSGS, [], max_tokens=10, temperature=None))
    assert events[-1].message.content == "from b" and (a.calls, b.calls) == (0, 1)


# ── a long deadline caps the stall limits that could never fire inside it ───────────────────────────────────────────


def test_stall_limits_that_outlast_a_call_deadline_are_cut_to_a_share_of_it():
    from k3code.providers.stall import CALL_DEADLINE, DEADLINE_SHARE, StreamWatchdog

    token = CALL_DEADLINE.set(90.0)
    try:
        local = StreamWatchdog(first_event=300.0, idle=300.0)  # a local server's limits
        remote = StreamWatchdog(first_event=60.0, idle=120.0)  # the defaults: the first fires inside 90 s, idle not
    finally:
        CALL_DEADLINE.reset(token)
    assert (local.first_event, local.idle) == (90.0 * DEADLINE_SHARE, 90.0 * DEADLINE_SHARE)
    assert (remote.first_event, remote.idle) == (60.0, 90.0 * DEADLINE_SHARE)
    assert StreamWatchdog(first_event=300.0, idle=300.0).first_event == 300.0  # no deadline: as configured


async def test_only_a_call_with_a_long_deadline_sets_it_for_the_providers():
    from k3code.providers.stall import CALL_DEADLINE

    seen: list[float | None] = []

    class Spy(Entry):
        async def stream(self, messages, tools, model, **kwargs):
            seen.append(CALL_DEADLINE.get())
            async for event in super().stream(messages, tools, model, **kwargs):
                yield event

    books = SimpleNamespace(rows=[], db=None)
    router = Router(build_chain([Spy("a"), Entry("b")], [["m"], ["m"]]), sleep=no_sleep)
    caller = ModelCaller(
        lambda: {t: router for t in Tier},
        SimpleNamespace(task_tiers=None, providers=[]),
        SimpleNamespace(record=lambda kind, **kw: books.rows.append(kind)),
    )
    await caller.complete(TaskKind.COMPACTION, MSGS, session_id="s1", timeout=90.0)
    await caller.complete(TaskKind.TITLE, MSGS, session_id="s1", timeout=15.0)
    await caller.complete(TaskKind.TITLE, MSGS, session_id="s1")
    assert seen == [90.0, None, None]
    assert CALL_DEADLINE.get() is None  # reset afterwards


async def test_a_hung_local_primary_fails_over_inside_a_long_deadline():
    """A keyless/local entry has a 300 s stall limit; a 90 s compaction deadline cut it off before it could fire, so the
    second entry was never tried. The watchdog the entry builds now gets 90 s x the share (0.6) as its first-event
    limit; the stub reports it and stalls at once."""
    import k3code.routing.caller as caller_mod
    from k3code.providers.stall import StreamWatchdog

    class Hangs(Entry):
        async def stream(self, messages, tools, model, **kwargs):
            wd = StreamWatchdog(first_event=300.0, idle=300.0)  # what the provider builds for a local server
            self.limit = wd.first_event
            await asyncio.sleep(0)  # (the real guard would now wait for the first byte)
            raise stalled()
            yield  # pragma: no cover

    a, b = Hangs("a"), Entry("b", reply="from b")
    router = router_of([a, b])
    books = SimpleNamespace(rows=[])
    caller = ModelCaller(
        lambda: {t: router for t in Tier},
        SimpleNamespace(task_tiers=None, providers=[]),
        SimpleNamespace(record=lambda kind, **kw: books.rows.append(kind)),
    )
    assert caller_mod.LONG_DEADLINE_S <= 90.0
    reply = await caller.complete(TaskKind.COMPACTION, MSGS, session_id="s1", timeout=90.0)
    assert reply.text == "from b" and a.limit == 90.0 * 0.6


# ── the second pass over dead-window entries: where it must not run, and what it must not change ────────────────────


def rate_limited() -> ProviderError:
    return ProviderError(
        "rate limited", status_code=429, headers={"Retry-After": "3600"}, body={"error": {"message": "rate limited"}}
    )


def unknown_model() -> ProviderError:
    return ProviderError("unknown model", status_code=400)


def offline() -> Exception:
    return to_provider_error(httpx.ConnectError("refused"), kind="openai")


async def test_a_call_under_a_deadline_does_not_retry_the_entries_it_skipped():
    """The second pass would run inside the deadline and leave the tier escalation (a fresh deadline) no time."""
    a, b = Entry("a", reply="from a"), Entry("b", fail=stalled)
    router = router_of([a, b])
    _arm_window(router, 0)
    with pytest.raises(ChainExhausted):
        await drain(router.stream(MSGS, [], max_tokens=10, temperature=None, retry_deferred=False))
    assert a.calls == 0
    events = await drain(router.stream(MSGS, [], max_tokens=10, temperature=None))  # without the switch: tried
    assert events[-1].message.content == "from a"


async def test_a_deadline_bound_call_asks_the_router_not_to_retry_skipped_entries():
    from k3code.routing.caller import ModelCaller

    seen: list[dict] = []

    class Spy:
        chain = [object()]

        async def stream(self, messages, tools, **kwargs):
            seen.append(kwargs)
            yield StreamEvent(type="done", message=Message(role="assistant", content="ok"))

    caller = ModelCaller(
        lambda: {t: Spy() for t in Tier},
        SimpleNamespace(task_tiers=None, providers=[]),
        SimpleNamespace(record=lambda kind, **kw: None),
    )
    await caller.complete(TaskKind.TITLE, MSGS, session_id="s1", timeout=15.0)
    await caller.complete(TaskKind.TITLE, MSGS, session_id="s1")
    assert seen[0].get("retry_deferred") is False and seen[0].get("stall_retries") == 0
    assert "retry_deferred" not in seen[1]


async def test_a_retried_entry_does_not_hide_the_reset_another_entry_is_waiting_for():
    """A (in a window) answers 400 once tried; B is rate-limited for an hour. The chain must still be a wait, not a
    bad request, whether or not A happened to be in a window."""
    for armed in (True, False):
        a, b = Entry("a", fail=unknown_model), Entry("b", fail=rate_limited)
        router = router_of([a, b])
        if armed:
            _arm_window(router, 0)
        with pytest.raises(ChainExhausted) as err:
            await drain(router.stream(MSGS, [], max_tokens=10, temperature=None))
        assert err.value.last_reason == "rate_limit", (
            armed
        )  # PersistentRetry parks on it (it fails hard on bad_request)


async def test_an_offline_chain_is_unreachable_even_when_an_entry_was_in_a_window():
    from k3code.errors import AllProvidersUnreachable

    for armed in (False, True):
        a, b = Entry("a", fail=offline), Entry("b", fail=offline)
        router = router_of([a, b])
        if armed:
            _arm_window(router, 0)
        with pytest.raises(AllProvidersUnreachable):
            await drain(router.stream(MSGS, [], max_tokens=10, temperature=None))
