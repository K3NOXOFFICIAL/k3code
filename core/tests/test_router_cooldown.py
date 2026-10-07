"""Long Retry-After / quota → cooldown + immediate failover; all-cooling → park; cooldowns persist."""

from __future__ import annotations

import json
import time

import pytest

from k3code.errors import ChainExhausted
from k3code.providers.base import ProviderError
from k3code.providers.types import Message, StreamEvent
from k3code.reliability.events import PARKED, UNPARKED, EventEmitter
from k3code.reliability.persistent_retry import PersistentRetry, RetryConfig
from k3code.router import CooldownStore, Router, RouterEvent, build_chain
from k3code.router.classifier import FailoverReason

QUOTA_BODY = {
    "error": {"message": "usage limit exceeded", "type": "usage_limit_exceeded", "code": "usage_limit_exceeded"}
}
MSGS = [Message(role="user", content="hi")]


class Scripted:
    """Provider that raises/streams per call: items are ProviderError | str (reply text)."""

    def __init__(self, name: str, script: list[ProviderError | str]) -> None:
        self.name = name
        self.base_url = f"http://{name}"
        self.script = script
        self.calls = 0

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, ProviderError):
            raise item
        yield StreamEvent(type="done", message=Message(role="assistant", content=item))


def err429(retry_after: float | None, body: dict | None = None) -> ProviderError:
    headers = {"Retry-After": str(int(retry_after))} if retry_after is not None else {}
    body = body or {"error": {"message": "rate limited"}}
    return ProviderError(message=body["error"]["message"], status_code=429, headers=headers, body=body)


def make(providers, **kw):
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    events: list[RouterEvent] = []
    store = kw.pop("store", None) or CooldownStore()
    router = Router(build_chain(providers, [["m"]] * len(providers)), cooldowns=store, sleep=fake_sleep,
                    on_event=events.append, **kw)
    return router, store, sleeps, events


async def test_long_retry_after_fails_over_without_sleeping():
    a = Scripted("a", [err429(59826)])
    b = Scripted("b", ["from b"])
    router, store, sleeps, events = make([a, b])
    msg = await router.complete(MSGS, [])
    assert msg.content == "from b"
    assert sleeps == [] and a.calls == 1  # no inline wait, no retry on the exhausted entry
    assert store.in_cooldown(provider="a", model="m", base_url="http://a")
    assert 59000 < store.remaining_seconds(provider="a", model="m", base_url="http://a") <= 59826
    assert any(e.kind == "router.cooldown" and e.extra["seconds"] == 59826 for e in events)


async def test_short_retry_after_still_sleeps_inline():
    a = Scripted("a", [err429(5), "ok after wait"])
    router, store, sleeps, _ = make([a], max_retries=1)
    assert (await router.complete(MSGS, [])).content == "ok after wait"
    assert sleeps == [5.0]
    assert not store.in_cooldown(provider="a", model="m", base_url="http://a")


async def test_max_inline_wait_is_configurable():
    a = Scripted("a", [err429(30)])
    b = Scripted("b", ["b"])
    router, _, sleeps, _ = make([a, b], max_inline_wait=60.0, max_retries=0)
    await router.complete(MSGS, [])  # 30 <= 60: retries disabled → fails over by exhaustion, no cooldown ladder
    router2, store2, sleeps2, _ = make([Scripted("a", [err429(30)]), Scripted("b", ["b"])], max_inline_wait=10.0)
    await router2.complete(MSGS, [])
    assert sleeps2 == [] and store2.in_cooldown(provider="a", model="m", base_url="http://a")


async def test_quota_without_retry_after_cools_down_for_an_hour():
    a = Scripted("a", [err429(None, QUOTA_BODY)])
    b = Scripted("b", ["b"])
    router, store, sleeps, _ = make([a, b])
    await router.complete(MSGS, [])
    remaining = store.remaining_seconds(provider="a", model="m", base_url="http://a")
    assert sleeps == [] and 3500 < remaining <= 3600
    entry = next(iter(store.entries.values()))
    assert entry.reason is FailoverReason.quota


async def test_quota_cooldown_default_is_configurable():
    a = Scripted("a", [err429(None, QUOTA_BODY)])
    router, store, _, _ = make([a, Scripted("b", ["b"])], quota_cooldown=120.0)
    await router.complete(MSGS, [])
    assert store.remaining_seconds(provider="a", model="m", base_url="http://a") <= 120


async def test_cooled_entry_is_skipped_on_next_call():
    a = Scripted("a", [err429(59826)])
    b = Scripted("b", ["b1", "b2"])
    router, _, _, _ = make([a, b])
    await router.complete(MSGS, [])
    await router.complete(MSGS, [])
    assert a.calls == 1 and b.calls == 2


async def test_all_cooling_fails_fast_with_clear_message():
    a = Scripted("a", [err429(7200)])
    b = Scripted("b", [err429(3600)])
    router, _, sleeps, _ = make([a, b])
    with pytest.raises(ChainExhausted) as ei:
        await router.complete(MSGS, [])
    exc = ei.value
    assert sleeps == []
    assert exc.retry_after is not None and 3500 < exc.retry_after <= 3600  # the earliest reset
    clock = time.strftime("%H:%M", time.localtime(exc.until))
    assert f"all providers rate-limited until {clock}" in str(exc)
    # a second call does not touch the providers at all
    with pytest.raises(ChainExhausted, match="all providers rate-limited until"):
        await router.complete(MSGS, [])
    assert a.calls == 1 and b.calls == 1


async def test_not_all_cooling_has_no_retry_after():
    a = Scripted("a", [err429(7200)])
    b = Scripted("b", [ProviderError(message="bad key", status_code=401)])
    router, _, _, _ = make([a, b])
    with pytest.raises(ChainExhausted) as ei:
        await router.complete(MSGS, [])
    assert ei.value.retry_after is None


async def test_persistent_retry_parks_until_earliest_reset_and_emits_time():
    a = Scripted("a", [err429(3600), "recovered"])
    router, store, _, _ = make([a])
    clock = {"now": 1000.0}
    slept: list[float] = []

    async def sleep(d: float) -> None:
        slept.append(d)
        clock["now"] += d
        if clock["now"] >= 1000.0 + 3599:  # the provider's window passed: expire the cooldown
            for e in store.entries.values():
                e.until = 0.0

    emitter = EventEmitter()
    seen: list[tuple[str, dict]] = []
    emitter.add(lambda e: seen.append((e.kind, {**e.data, "detail": e.detail})), key="t")
    pr = PersistentRetry(router, None, config=RetryConfig(poll_interval=600.0), events=emitter,
                         sleep=sleep, monotonic=lambda: clock["now"])
    texts = []
    async for ev in pr.stream(MSGS, []):
        if ev.type == "done" and ev.message:
            texts.append(ev.message.content)
    assert texts == ["recovered"]
    parked = [d for k, d in seen if k == PARKED]
    assert len(parked) == 1
    assert 3500 < parked[0]["delay"] <= 3600 and parked[0]["until"] > time.time()
    assert "all providers rate-limited until" in parked[0]["detail"]
    assert [k for k, _ in seen] == [PARKED, UNPARKED]
    assert a.calls == 2


# ── persistence ──


def test_cooldowns_persist_across_restart(tmp_path):
    path = tmp_path / "cooldowns.json"
    store = CooldownStore(path=path)
    store.arm(FailoverReason.quota, provider="a", model="m", base_url="http://a", retry_after=5000)
    store.arm(FailoverReason.network, provider="n", model="m", base_url="http://n")  # not persisted
    data = json.loads(path.read_text())
    assert [r["provider"] for r in data["entries"]] == ["a"]

    reborn = CooldownStore(path=path)  # daemon restart
    assert reborn.in_cooldown(provider="a", model="m", base_url="http://a")
    assert 4900 < reborn.remaining_seconds(provider="a", model="m", base_url="http://a") <= 5000
    assert not reborn.in_cooldown(provider="n", model="m", base_url="http://n")


def test_expired_cooldowns_are_dropped_on_load(tmp_path):
    path = tmp_path / "cooldowns.json"
    wall = {"t": 1_000_000.0}
    store = CooldownStore(path=path, wall=lambda: wall["t"])
    store.arm(FailoverReason.quota, provider="a", model="m", retry_after=100)
    wall["t"] += 101
    assert not CooldownStore(path=path, wall=lambda: wall["t"]).in_cooldown(provider="a", model="m")


def test_corrupt_cooldown_file_is_ignored(tmp_path):
    path = tmp_path / "cooldowns.json"
    path.write_text("{not json")
    assert CooldownStore(path=path).entries == {}


async def test_restarted_router_does_not_hit_exhausted_provider(tmp_path):
    path = tmp_path / "cooldowns.json"
    a = Scripted("a", [err429(59826)])
    router, _, _, _ = make([a, Scripted("b", ["b"])], store=CooldownStore(path=path))
    await router.complete(MSGS, [])
    a2 = Scripted("a", ["a is back?"])  # the same provider after a daemon restart
    router2, _, _, _ = make([a2, Scripted("b", ["b"])], store=CooldownStore(path=path))
    assert (await router2.complete(MSGS, [])).content == "b"
    assert a2.calls == 0


def test_clear_reason_drops_only_that_reason():
    from k3code.router.classifier import FailoverReason
    from k3code.router.cooldown import CooldownStore

    store = CooldownStore()
    store.arm(FailoverReason.network, provider="a", model="m", base_url="u", network_cooldown=60)
    store.arm(FailoverReason.rate_limit, provider="b", model="m", base_url="u", retry_after=60)
    assert store.clear_reason(FailoverReason.network) == 1
    assert not store.in_cooldown(provider="a", model="m", base_url="u")
    assert store.in_cooldown(provider="b", model="m", base_url="u")
