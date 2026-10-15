"""The per-entry circuit breaker for server/timeout/unknown failures (issue #125).

Before it, every model call during a 5xx outage of the primary re-tried the dead entry 3 times with ~7 s of backoff
before the backup was used, and nothing cooled the entry afterwards.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from k3code.errors import ChainExhausted
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message
from k3code.router import CooldownStore, Router, RouterEvent, build_chain
from k3code.router.classifier import FailoverReason

_OK = (
    'data: {"choices":[{"delta":{"content":"Hi"}}]}\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
    "data: [DONE]\n"
)


def _provider(name: str, responses) -> tuple[OpenAICompatProvider, AsyncMock]:
    client = AsyncMock(spec=httpx.AsyncClient)
    client.send = AsyncMock(side_effect=responses)
    client.build_request = MagicMock(return_value=MagicMock())
    client.aclose = AsyncMock()
    return OpenAICompatProvider(name=name, base_url=f"https://{name}.test", api_key="k", client=client), client


def _dead(status: int = 503, headers: dict[str, str] | None = None):
    def respond(*_a, **_k):
        return httpx.Response(status, headers=headers or {}, json={"error": {"message": "overloaded"}})

    return respond


def _ok(*_a, **_k):
    return httpx.Response(200, text=_OK, headers={"content-type": "text/event-stream"})


def _setup(primary_responses=None, *, backup: bool = True):
    primary, primary_client = _provider("primary", primary_responses or _dead())
    providers, specs = [primary], [["m1"]]
    if backup:
        secondary, _ = _provider("backup", _ok)
        providers.append(secondary)
        specs.append(["m2"])
    events: list[RouterEvent] = []
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    store = CooldownStore()
    router = Router(build_chain(providers, specs), cooldowns=store, on_event=events.append, sleep=sleep)
    return router, store, primary_client, events, sleeps


def _expire(store: CooldownStore) -> None:
    """End every window now (the monotonic deadline passes, the wall deadline is dropped)."""
    for entry in store.entries.values():
        entry.until = time.monotonic() - 1
        entry.until_wall = 0.0


async def test_a_failed_call_opens_the_breaker_so_the_next_calls_skip_the_dead_entry():
    router, store, primary_client, events, sleeps = _setup()
    msgs = [Message(role="user", content="hi")]

    assert (await router.complete(msgs, [])).content == "Hi"
    assert primary_client.send.await_count == 3  # the first call still pays the retries
    assert [e.kind for e in events if e.kind == "router.cooldown"] == ["router.cooldown"]
    assert events[[e.kind for e in events].index("router.cooldown")].extra["seconds"] == 15

    sleeps.clear()
    assert (await router.complete(msgs, [])).content == "Hi"
    assert primary_client.send.await_count == 3  # not touched again while the breaker is open
    assert sleeps == []  # and no backoff paid


async def test_the_half_open_probe_is_one_request_and_a_failure_doubles_the_window():
    router, store, primary_client, events, _ = _setup()
    msgs = [Message(role="user", content="hi")]
    await router.complete(msgs, [])
    _expire(store)

    await router.complete(msgs, [])
    assert primary_client.send.await_count == 4  # one probe, no inline retries
    windows = [e.extra["seconds"] for e in events if e.kind == "router.cooldown"]
    assert windows == [15, 30]
    _expire(store)
    await router.complete(msgs, [])
    await router.complete(msgs, [])  # still open: no probe
    assert primary_client.send.await_count == 5
    assert [e.extra["seconds"] for e in events if e.kind == "router.cooldown"] == [15, 30, 60]


async def test_a_success_closes_the_breaker_and_resets_its_ladder():
    router, store, primary_client, events, _ = _setup([_dead()(), _dead()(), _dead()(), _ok()])
    msgs = [Message(role="user", content="hi")]
    await router.complete(msgs, [])
    _expire(store)
    await router.complete(msgs, [])  # the probe answers
    assert primary_client.send.await_count == 4
    assert not store.tripped(
        provider="primary", model="m1", base_url="https://primary.test", account=router.chain[0].key_fingerprint
    )


async def test_a_retry_after_on_a_503_seeds_the_window():
    router, _store, _client, events, _ = _setup(_dead(503, {"Retry-After": "45"}))
    await router.complete([Message(role="user", content="hi")], [])
    assert [e.extra["seconds"] for e in events if e.kind == "router.cooldown"] == [45]


async def test_a_one_entry_chain_fails_the_call_at_once_and_the_next_one_waits_for_the_window():
    router, _store, client, _events, _ = _setup(backup=False)
    msgs = [Message(role="user", content="hi")]
    with pytest.raises(ChainExhausted) as first:
        await router.complete(msgs, [])
    assert client.send.await_count == 3
    assert first.value.last_reason == "server" and first.value.retry_after is None  # as before: no silent park
    with pytest.raises(ChainExhausted) as second:
        await router.complete(msgs, [])
    assert client.send.await_count == 3  # the open breaker: not tried again
    assert second.value.retry_after is not None and 0 < second.value.retry_after <= 15
    assert "cooling down after failures until" in str(second.value) and "rate-limited" not in str(second.value)


def test_breaker_windows_are_not_persisted(tmp_path):
    store = CooldownStore(path=tmp_path / "cooldowns.json")
    assert store.arm(FailoverReason.server, provider="p", model="m") == 15
    assert store.arm(FailoverReason.rate_limit, provider="p", model="m2", retry_after=90) == 90
    reloaded = CooldownStore(path=tmp_path / "cooldowns.json")
    assert [k[1] for k in reloaded.entries] == ["m2"]


def test_breaker_and_rate_limit_ladders_are_independent():
    store = CooldownStore()
    store.arm(FailoverReason.rate_limit, provider="p", model="m")
    store.arm(FailoverReason.rate_limit, provider="p", model="m")
    assert store.arm(FailoverReason.timeout, provider="p", model="m") == 15  # a 429 history does not climb it
    assert store.arm(FailoverReason.unknown, provider="p", model="m") == 30
    store.record_success(provider="p", model="m")
    assert store.arm(FailoverReason.server, provider="p", model="m") == 15
