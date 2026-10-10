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
        self.api_key = f"key-{name}"
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
    router = Router(
        build_chain(providers, [["m"]] * len(providers)),
        cooldowns=store,
        sleep=fake_sleep,
        on_event=events.append,
        **kw,
    )
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
    b = Scripted("b", [ProviderError(message="bad request", status_code=400)])  # fails over, arms no cooldown
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
    pr = PersistentRetry(
        router,
        None,
        config=RetryConfig(poll_interval=600.0),
        events=emitter,
        sleep=sleep,
        monotonic=lambda: clock["now"],
    )
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


async def test_rate_limit_cooldown_ladder_climbs_and_resets_on_success():
    """arm() always used backoff_count=0: an entry rate-limited again and again cooled down 60 s every time."""
    store = CooldownStore()
    arm = [store.arm(FailoverReason.rate_limit, provider="a", model="m", base_url="http://a") for _ in range(3)]
    assert arm == [60, 120, 240]
    store.arm(FailoverReason.rate_limit, provider="b", model="m", base_url="http://b")  # per entry
    assert store.strikes[("b", "m", "http://b")] == 1
    a = Scripted("a", ["ok"])
    router, _, _, _ = make([a], store=store)
    store.entries.clear()
    assert (await router.complete(MSGS, [])).content == "ok"
    assert store.arm(FailoverReason.rate_limit, provider="a", model="m", base_url="http://a") == 60


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


async def test_a_cooldown_armed_under_a_model_override_is_honoured_on_the_next_call():
    a = Scripted("a", [err429(120)])
    router, store, _, _ = make([a])
    with pytest.raises(ChainExhausted):
        await router.complete(MSGS, [], model="override")
    assert store.in_cooldown(provider="a", model="override", base_url="http://a")
    with pytest.raises(ChainExhausted) as exc:
        await router.complete(MSGS, [], model="override")
    assert a.calls == 1  # skipped inside its window, not called again
    assert exc.value.retry_after is not None and exc.value.retry_after > 100


# ── auth failures cool the entry down ──────────────────────────────────


def err401() -> ProviderError:
    return ProviderError(message="invalid api key", status_code=401, body={"error": {"message": "invalid api key"}})


async def test_auth_failure_cools_the_entry_down_and_later_calls_skip_it():
    a = Scripted("a", [err401()])
    b = Scripted("b", ["from b"])
    router, store, _, _ = make([a, b])
    assert (await router.complete(MSGS, [])).content == "from b"
    assert (await router.complete(MSGS, [])).content == "from b"  # the second rejection in a row arms it
    assert a.calls == 2
    assert store.reason_of(provider="a", model="m", base_url="http://a") is FailoverReason.auth
    assert 290 < store.remaining_seconds(provider="a", model="m", base_url="http://a") <= 300
    assert (await router.complete(MSGS, [])).content == "from b"
    assert a.calls == 2  # no POST (and so no reachability probe) to the dead entry on the third call


async def test_replaced_key_is_tried_at_once():
    a = Scripted("a", [err401(), err401(), "a works now"])
    a.api_key = "revoked-key"
    router, store, _, _ = make([a, Scripted("b", ["from b"])])
    assert (await router.complete(MSGS, [])).content == "from b"
    assert (await router.complete(MSGS, [])).content == "from b"
    assert (await router.complete(MSGS, [])).content == "from b" and a.calls == 2
    a.api_key = "fresh-key"
    assert (await router.complete(MSGS, [])).content == "a works now"
    assert a.calls == 3
    assert store.reason_of(provider="a", model="m", base_url="http://a") is None


def test_auth_cooldown_never_stores_the_key(tmp_path):
    from k3code.router.cooldown import key_fingerprint

    path = tmp_path / "cooldowns.json"
    store = CooldownStore(path=path)
    for _ in range(2):
        store.arm(FailoverReason.auth, provider="a", model="m", fingerprint=key_fingerprint("sk-secret-value"))
    raw = path.read_text()
    assert "sk-secret-value" not in raw and key_fingerprint("sk-secret-value") in raw
    reborn = CooldownStore(path=path)
    assert reborn.in_cooldown(provider="a", model="m", fingerprint=key_fingerprint("sk-secret-value"))
    assert not reborn.in_cooldown(provider="a", model="m", fingerprint=key_fingerprint("other"))


def test_auth_ladder_doubles_to_an_hour_and_success_resets_it():
    store = CooldownStore()
    seen = [store.arm(FailoverReason.auth, provider="a", model="m", retry_after=5) for _ in range(7)]
    assert seen == [None, 300, 600, 1200, 2400, 3600, 3600]  # a declared Retry-After does not shorten it
    store.record_success(provider="a", model="m")
    assert [store.arm(FailoverReason.auth, provider="a", model="m") for _ in range(2)] == [None, 300]


def test_clear_reason_auth_leaves_other_cooldowns_and_resets_the_ladder():
    store = CooldownStore()
    for _ in range(3):
        store.arm(FailoverReason.auth, provider="a", model="m")
    store.arm(FailoverReason.quota, provider="q", model="m")
    assert store.clear_reason(FailoverReason.auth) == 1
    assert store.reason_of(provider="q", model="m") is FailoverReason.quota
    assert [store.arm(FailoverReason.auth, provider="a", model="m") for _ in range(2)] == [None, 300]


async def test_all_entries_in_auth_cooldown_says_authentication_not_rate_limit():
    a, b = Scripted("a", [err401()]), Scripted("b", [err401()])
    router, _, _, _ = make([a, b])
    with pytest.raises(ChainExhausted) as first:
        await router.complete(MSGS, [])
    with pytest.raises(ChainExhausted) as second:  # the second rejection in a row arms both
        await router.complete(MSGS, [])
    with pytest.raises(ChainExhausted) as third:  # now both are skipped, nothing is called
        await router.complete(MSGS, [])
    assert (a.calls, b.calls) == (2, 2)
    for exc in (first.value, second.value, third.value):
        assert "authentication" in str(exc) and "rate-limited" not in str(exc)
        assert exc.last_reason == "auth" and exc.retry_after is None  # permanent: the retry layer must not park


async def test_one_transient_rejection_arms_no_cooldown_two_in_a_row_do():
    a = Scripted("a", [err401(), "a works", err401(), err401(), "unreachable"])
    router, store, _, _ = make([a])
    ident = {"provider": "a", "model": "m", "base_url": "http://a"}
    with pytest.raises(ChainExhausted):
        await router.complete(MSGS, [])
    assert store.reason_of(**ident) is None  # one 401 from a restarting relay: the next prompt is tried
    assert (await router.complete(MSGS, [])).content == "a works"  # success resets the strike
    with pytest.raises(ChainExhausted):
        await router.complete(MSGS, [])
    assert store.reason_of(**ident) is None  # still only one strike since the success
    with pytest.raises(ChainExhausted):
        await router.complete(MSGS, [])
    assert store.reason_of(**ident) is FailoverReason.auth


async def test_a_keyless_entry_never_arms_an_auth_cooldown():
    a = Scripted("a", [err401()])
    a.api_key = ""  # claude-cli: logged in with `claude /login`, nothing in the config
    router, store, _, _ = make([a])
    for _ in range(3):
        with pytest.raises(ChainExhausted):
            await router.complete(MSGS, [])
    assert a.calls == 3  # tried every time, never skipped
    assert store.reason_of(provider="a", model="m", base_url="http://a") is None


async def test_mixed_rate_limit_and_auth_chain_waits_for_the_reset_instead_of_calling_it_auth():
    a = Scripted("a", [err429(120)])
    b = Scripted("b", [err401()])
    router, _, _, _ = make([a, b])
    with pytest.raises(ChainExhausted) as exc:
        await router.complete(MSGS, [])
    assert "rate-limited" in str(exc.value) and "authentication" not in str(exc.value)
    assert exc.value.retry_after is not None and 100 < exc.value.retry_after <= 120  # parks, resumes when A resets
    assert exc.value.last_reason == "auth"


async def test_a_chain_where_every_failure_is_auth_still_says_authentication_failed():
    router, _, _, _ = make([Scripted("a", [err401()]), Scripted("b", [err401()])])
    with pytest.raises(ChainExhausted) as exc:
        await router.complete(MSGS, [])
    assert "authentication failed" in str(exc.value) and exc.value.retry_after is None
