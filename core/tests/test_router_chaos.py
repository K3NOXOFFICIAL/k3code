"""Chaos tests for the fallback walk: scripted faults per call on every entry of a chain (GOAL.md C1/C3)."""

from __future__ import annotations

import pytest

from k3code.errors import ContextOverflow
from k3code.providers.base import ProviderError
from k3code.providers.types import Message, StreamEvent
from k3code.router import CooldownStore, Router, RouterEvent, build_chain


class Stub:
    """A provider that plays one scripted step per ``stream()`` call: an exception is raised, a string is the answer.
    Every call records the model and the max_tokens it was sent."""

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
        yield StreamEvent(type="text_delta", text=step)
        yield StreamEvent(type="done", message=Message(role="assistant", content=step))


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
