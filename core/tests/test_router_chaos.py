"""Chaos tests for the fallback walk: scripted faults per call on every entry of a chain (GOAL.md C1/C3)."""

from __future__ import annotations

import pytest

from k3code.errors import ContextOverflow
from k3code.providers.base import ProviderError
from k3code.providers.types import Message, StreamEvent
from k3code.router import CooldownStore, FailoverReason, Router, RouterEvent, build_chain


class Stub:
    """A provider that plays one scripted step per ``stream()`` call: an exception is raised, a string or a Message is
    the answer. Every call records the model and the max_tokens it was sent."""

    def __init__(self, name: str, script: list | None = None) -> None:
        self.name = name
        self.base_url = f"stub://{name}"
        self.api_key = f"key-{name}"
        self.script = list(script or [])
        self.calls: list[dict] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.calls.append({"model": model, "max_tokens": max_tokens})
        step = self.script.pop(0) if self.script else f"answer from {self.name}"
        if isinstance(step, BaseException):
            raise step
        message = step if isinstance(step, Message) else Message(role="assistant", content=step)
        yield StreamEvent(type="text_delta", text=message.content)
        yield StreamEvent(type="done", message=message)


def prompt(tokens: int) -> list[Message]:
    """A conversation the context_budget estimator puts at ``tokens`` tokens (4 ascii chars per token)."""
    return [Message(role="user", content="x" * (tokens * 4))]


async def _no_sleep(_seconds: float) -> None:
    return None


def make(providers, models, **kw) -> tuple[Router, list[RouterEvent]]:
    events: list[RouterEvent] = []
    router = Router(
        build_chain(providers, models),
        max_retries=2,
        cooldowns=kw.pop("cooldowns", None) or CooldownStore(),
        on_event=events.append,
        sleep=_no_sleep,
        **kw,
    )
    return router, events


# ── context windows ─────────────────────────────────────────────────────


async def test_an_overflow_on_a_200k_window_fails_over_to_the_1m_window():
    """The walk used to raise ContextOverflow on the first overflow, so a chain with a larger window behind it
    compacted (or failed) instead of letting that entry answer."""
    a = Stub("a", [ProviderError("prompt is too long: 210000 tokens > 200000 maximum", status_code=400)])
    b = Stub("b")
    router, _ = make([a, b], [["claude-a"], ["big-b"]], context_windows={"big-b": 1_000_000})
    msg = await router.complete(prompt(150_000), [])
    assert msg.content == "answer from b"
    assert len(a.calls) == 1 and len(b.calls) == 1


async def test_an_overflow_with_no_larger_window_left_still_asks_for_compaction():
    a = Stub("a", [ProviderError("prompt is too long: 210000 tokens > 200000 maximum", status_code=400)])
    b = Stub("b")
    router, _ = make([a, b], [["claude-a"], ["claude-b"]])  # both 200k
    with pytest.raises(ContextOverflow):
        await router.complete(prompt(150_000), [])
    assert b.calls == []


async def test_an_overflow_whose_larger_window_then_fails_asks_for_compaction():
    """Not ChainExhausted: compacting lets the smaller window answer."""
    overflow = ProviderError("prompt is too long: 210000 tokens > 200000 maximum", status_code=400)
    a = Stub("a", [overflow])
    b = Stub("b", [ProviderError("upstream down", status_code=500)] * 3)
    router, _ = make([a, b], [["claude-a"], ["big-b"]], context_windows={"big-b": 1_000_000})
    with pytest.raises(ContextOverflow):
        await router.complete(prompt(150_000), [])
    assert len(b.calls) == 3


@pytest.mark.parametrize(
    "error",
    [
        ProviderError(
            "input length and `max_tokens` exceed context limit: 150000 + 64000 > 200000, decrease input length "
            "or `max_tokens` and try again",
            status_code=400,
        ),
        ProviderError(
            "This model's maximum context length is 200000 tokens. However, you requested 214000 tokens (150000 in "
            "the messages, 64000 in the completion). Please reduce the length of the messages or completion.",
            status_code=400,
            body={"error": {"code": "context_length_exceeded"}},
        ),
    ],
    ids=["anthropic", "openai"],
)
async def test_a_max_tokens_overflow_retries_the_same_entry_once_with_a_clamped_max_tokens(error):
    """The prompt fits, only prompt + max_tokens does not: the same entry answers with less output room (the
    Anthropic wording used to be a bad_request failover, the OpenAI one a ContextOverflow)."""
    a = Stub("a", [error])
    b = Stub("b")
    router, events = make([a, b], [["gw-model"], ["gw-other"]])
    msg = await router.complete(prompt(10), [], max_tokens=64_000)
    assert msg.content == "answer from a"
    assert [c["max_tokens"] for c in a.calls] == [64_000, 200_000 - 150_000 - 1024]
    assert b.calls == []
    assert [e.kind for e in events if e.kind == "router.failover"] == []


async def test_a_second_max_tokens_overflow_on_the_clamped_retry_is_an_overflow():
    error = ProviderError(
        "input length and `max_tokens` exceed context limit: 150000 + 64000 > 200000", status_code=400
    )
    a = Stub("a", [error, error])
    router, _ = make([a], [["gw-model"]])
    with pytest.raises(ContextOverflow):
        await router.complete(prompt(10), [], max_tokens=64_000)
    assert len(a.calls) == 2


async def test_an_entry_whose_window_cannot_hold_the_prompt_is_passed_over_before_any_call():
    a, b = Stub("a"), Stub("b")
    router, events = make([a, b], [["claude-a"], ["big-b"]], context_windows={"big-b": 1_000_000})
    msg = await router.complete(prompt(250_000), [])
    assert msg.content == "answer from b"
    assert a.calls == []
    assert [(e.kind, e.provider, e.reason) for e in events if e.kind == "router.failover"] == [
        ("router.failover", "a", "context_overflow")
    ]


async def test_a_too_small_window_with_nothing_larger_is_still_tried():
    """The estimate is rough: with no larger window left the provider decides."""
    a = Stub("a")
    router, _ = make([a], [["claude-a"]])
    assert (await router.complete(prompt(250_000), [])).content == "answer from a"


def test_configured_context_windows_reach_the_router():
    from types import SimpleNamespace

    from k3code.routing.tiers import router_options

    config = SimpleNamespace(router={}, models={"big-b": {"context_window": 1_000_000}, "plain": {}})
    assert router_options(config) == {"context_windows": {"big-b": 1_000_000}}


async def test_max_tokens_is_clamped_to_the_room_each_known_window_leaves():
    a, b = Stub("a", [ProviderError("upstream down", status_code=500)] * 3), Stub("b")
    router, _ = make([a, b], [["claude-a"], ["mystery-b"]])  # 200k family window, then an unknown one
    await router.complete(prompt(150_000), [], max_tokens=64_000)
    assert {c["max_tokens"] for c in a.calls} == {200_000 - 150_000 - 1024}
    assert [c["max_tokens"] for c in b.calls] == [64_000]  # unknown window: the request's own max_tokens


# ── cooldowns ───────────────────────────────────────────────────────────


def model_not_found() -> ProviderError:
    return ProviderError(
        "The model `gone-1` does not exist", status_code=404, body={"error": {"code": "model_not_found"}}
    )


async def test_an_unknown_model_id_is_skipped_first_on_the_next_call():
    """A dead model id was the first entry tried on every call: each one paid a 404 before the backup answered."""
    a, b = Stub("a", [model_not_found()]), Stub("b")
    router, events = make([a, b], [["gone-1"], ["live-1"]])
    assert (await router.complete(prompt(10), [])).content == "answer from b"
    assert (await router.complete(prompt(10), [])).content == "answer from b"
    assert len(a.calls) == 1
    assert [e.reason for e in events if e.kind == "router.cooldown"] == ["bad_request"]


async def test_a_dead_model_alone_fails_at_once_instead_of_waiting_for_its_cooldown():
    from k3code.errors import ChainExhausted

    a = Stub("a", [model_not_found()])
    router, _ = make([a], [["gone-1"]])
    with pytest.raises(ChainExhausted):
        await router.complete(prompt(10), [])
    with pytest.raises(ChainExhausted) as caught:
        await router.complete(prompt(10), [])
    assert caught.value.retry_after is None and caught.value.last_reason == "bad_request"
    assert len(a.calls) == 1


async def test_a_malformed_request_arms_no_cooldown():
    a = Stub("a", [ProviderError("unknown parameter: frobnicate", status_code=400), "fixed"])
    b = Stub("b")
    router, _ = make([a, b], [["m-a"], ["m-b"]])
    await router.complete(prompt(10), [])
    assert (await router.complete(prompt(10), [])).content == "fixed"


def rate_limited(seconds: int = 120) -> ProviderError:
    return ProviderError(
        "rate limited",
        status_code=429,
        headers={"Retry-After": str(seconds)},
        body={"error": {"message": "rate limited"}},
    )


async def test_two_routers_writing_one_cooldown_file_keep_each_others_entries(tmp_path):
    """Each process rewrote cooldowns.json from its own memory: the last writer dropped the other's cooldowns."""
    path = tmp_path / "cooldowns.json"
    store1, store2 = CooldownStore(path=path), CooldownStore(path=path)  # two processes, loaded before any write
    a, b, c, d = Stub("a", [rate_limited()]), Stub("b"), Stub("c", [model_not_found()]), Stub("d")
    r1, _ = make([a, b], [["m-a"], ["m-b"]], cooldowns=store1)
    r2, _ = make([c, d], [["m-c"], ["m-d"]], cooldowns=store2)
    await r1.complete(prompt(10), [])
    await r2.complete(prompt(10), [])
    reborn = CooldownStore(path=path)
    assert reborn.reason_of(provider="a", model="m-a", base_url="stub://a", account=r1.chain[0].key_fingerprint) == (
        FailoverReason.rate_limit
    )
    assert reborn.reason_of(provider="c", model="m-c", base_url="stub://c", account=r2.chain[0].key_fingerprint) == (
        FailoverReason.bad_request
    )


def test_a_cooldown_cleared_on_purpose_is_not_merged_back(tmp_path):
    path = tmp_path / "cooldowns.json"
    CooldownStore(path=path).arm(FailoverReason.quota, provider="a", model="m", retry_after=5000)
    CooldownStore(path=path).arm(FailoverReason.quota, provider="b", model="m", retry_after=5000)
    assert CooldownStore(path=path).clear_reason(FailoverReason.quota, provider="a") == 1
    reborn = CooldownStore(path=path)
    assert not reborn.in_cooldown(provider="a", model="m")
    assert reborn.in_cooldown(provider="b", model="m")


async def test_the_last_good_entry_survives_a_restart(tmp_path):
    path = tmp_path / "cooldowns.json"
    a, b = Stub("a", [ProviderError("upstream down", status_code=500)] * 3), Stub("b")
    router, _ = make([a, b], [["m-a"], ["m-b"]], cooldowns=CooldownStore(path=path))
    await router.complete(prompt(10), [])
    assert CooldownStore(path=path).last_good == router.chain[1].key


# ── filtered answers ────────────────────────────────────────────────────


@pytest.mark.parametrize("stop", ["content_filter", "refusal"])
async def test_a_filtered_answer_fails_over_to_the_next_entry(stop):
    a = Stub("a", [Message(role="assistant", content="[filtered]", stop_reason=stop)])
    b = Stub("b")
    router, events = make([a, b], [["m-a"], ["m-b"]])
    seen = [e async for e in router.stream(prompt(10), [])]
    assert [e.type for e in seen] == ["text_delta", "reset", "text_delta", "done"]
    assert seen[-1].message.content == "answer from b"
    assert [(e.provider, e.reason) for e in events if e.kind == "router.failover"] == [("a", "content_filter")]
    assert not [e for e in events if e.kind == "router.cooldown"]


async def test_the_last_entrys_refusal_is_passed_through():
    a = Stub("a", [Message(role="assistant", content="I can't help with that.", stop_reason="refusal")])
    router, _ = make([a], [["m-a"]])
    msg = await router.complete(prompt(10), [])
    assert msg.content == "I can't help with that." and msg.stop_reason == "refusal"
