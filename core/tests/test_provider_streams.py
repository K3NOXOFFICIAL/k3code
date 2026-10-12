"""A 200 response that dies mid-stream must fail, not become a successful (half-written) answer."""

from __future__ import annotations

import httpx
import pytest

from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.base import ProviderError
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message
from k3code.router.classifier import FailoverReason, classify_api_error

MSGS = [Message(role="user", content="hi")]


def client(body: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def openai(body: str) -> OpenAICompatProvider:
    return OpenAICompatProvider(name="o", base_url="https://o.test/v1", api_key="k", client=client(body))


def anthropic(body: str) -> AnthropicProvider:
    return AnthropicProvider(name="a", base_url="https://a.test", api_key="k", client=client(body))


async def run(provider):
    return [e async for e in provider.stream(MSGS, [], "m")]


OK_OPENAI = (
    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'
    'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
    "data: [DONE]\n\n"
)


async def test_openai_complete_stream_is_accepted():
    events = await run(openai(OK_OPENAI))
    assert events[-1].type == "done" and events[-1].message.content == "Hello world"
    assert events[-1].usage.completion_tokens == 2


async def test_openai_finish_reason_without_done_marker_is_complete():
    body = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
    assert (await run(openai(body)))[-1].message.content == "ok"


async def test_openai_truncated_stream_raises_a_retryable_error():
    # the connection closed: neither a finish_reason nor [DONE] ever arrived
    body = 'data: {"choices":[{"delta":{"content":"Half a sent"}}]}\n\n'
    with pytest.raises(ProviderError) as exc:
        await run(openai(body))
    # a peer disconnect: retried on the same entry, then failed over
    assert classify_api_error(exc.value, provider="o", model="m").reason == FailoverReason.server


async def test_openai_empty_body_is_not_a_successful_empty_answer():
    with pytest.raises(ProviderError):
        await run(openai(""))


async def test_openai_in_band_error_chunk_raises_with_the_upstream_message():
    body = 'data: {"error":{"message":"The server is overloaded","type":"overloaded_error","code":529}}\n\n'
    with pytest.raises(ProviderError) as exc:
        await run(openai(body))
    assert "overloaded" in exc.value.message and exc.value.status_code == 529
    assert classify_api_error(exc.value, provider="o", model="m").reason == FailoverReason.server


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ReadTimeout(""), FailoverReason.timeout),
        (httpx.ConnectTimeout(""), FailoverReason.timeout),
        (httpx.ConnectError(""), FailoverReason.network),
        (httpx.ReadError(""), FailoverReason.network),
    ],
)
async def test_wrapped_transport_errors_classify_by_their_cause(exc, reason):
    """httpx errors with an empty message became ProviderError("ReadTimeout") and classified as unknown."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    def failing() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    for provider in (
        OpenAICompatProvider(name="o", base_url="https://o.test/v1", api_key="k", client=failing()),
        AnthropicProvider(name="a", base_url="https://a.test", api_key="k", client=failing()),
    ):
        with pytest.raises(ProviderError) as err:
            await run(provider)
        assert classify_api_error(err.value, provider="o", model="m").reason == reason


HI_DELTA = '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}'
OK_ANTHROPIC = (
    'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
    f"event: content_block_delta\ndata: {HI_DELTA}\n\n"
    'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":1}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


async def test_anthropic_complete_stream_is_accepted():
    events = await run(anthropic(OK_ANTHROPIC))
    assert events[-1].type == "done" and events[-1].message.content == "Hi"


async def test_anthropic_stream_without_message_stop_raises():
    body = OK_ANTHROPIC.replace('event: message_stop\ndata: {"type":"message_stop"}\n\n', "")
    with pytest.raises(ProviderError):
        await run(anthropic(body))


async def test_anthropic_in_band_overloaded_error_raises_as_overloaded():
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
        'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    )
    with pytest.raises(ProviderError) as exc:
        await run(anthropic(body))
    assert exc.value.status_code == 529
    assert classify_api_error(exc.value, provider="a", model="m").reason == FailoverReason.server


async def test_openai_error_chunk_that_also_carries_choices_raises():
    # OpenRouter: a top-level error next to choices[0].finish_reason == "error"
    body = (
        'data: {"choices":[{"index":0,"delta":{"content":"Half an ans"}}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"content":""},"finish_reason":"error"}],'
        '"error":{"message":"Provider returned error","code":502}}\n\n'
        "data: [DONE]\n\n"
    )
    with pytest.raises(ProviderError) as exc:
        await run(openai(body))
    assert "Provider returned error" in exc.value.message and exc.value.status_code == 502


def test_cache_hits_are_read_from_every_openai_compatible_spelling():
    from k3code.providers.openai_compat import _parse_usage

    openai = _parse_usage(
        {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 60}}
    )
    deepseek = _parse_usage({"prompt_tokens": 100, "prompt_cache_hit_tokens": 70, "prompt_cache_miss_tokens": 30})
    moonshot = _parse_usage({"prompt_tokens": 100, "cached_tokens": 80})
    relay = _parse_usage({"prompt_tokens": 100, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10})
    none = _parse_usage({"prompt_tokens": 100})
    assert [u.cache_read_tokens for u in (openai, deepseek, moonshot, relay, none)] == [60, 70, 80, 50, 0]
    assert relay.cache_creation_tokens == 10


def test_openrouters_reported_cost_is_kept_only_for_openrouter():
    from k3code.providers.openai_compat import _parse_usage

    body = {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.0042}
    assert _parse_usage(body, reports_cost=True).cost_usd == 0.0042
    assert _parse_usage(body).cost_usd is None, "another server's `cost` field is not assumed to be dollars"
    assert _parse_usage({"prompt_tokens": 1, "cost": -1}, reports_cost=True).cost_usd is None


def test_an_openrouter_call_on_the_users_own_key_counts_the_upstream_charge_too():
    from k3code.providers.openai_compat import _parse_usage

    byok = {
        "prompt_tokens": 100_000,
        "cost": 0.0,
        "is_byok": True,
        "cost_details": {"upstream_inference_cost": 0.52},
    }
    assert _parse_usage(byok, reports_cost=True).cost_usd == pytest.approx(0.52)
    # without the flag the upstream figure is not added (it could already be inside `cost`)
    assert _parse_usage({**byok, "is_byok": False, "cost": 0.6}, reports_cost=True).cost_usd == pytest.approx(0.6)
    assert _parse_usage({**byok, "cost_details": ["not", "a", "dict"]}, reports_cost=True).cost_usd == 0.0


async def test_anthropic_input_counts_finalized_in_message_delta_are_used():
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":0}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"hi"}}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"input_tokens":7,"cache_read_input_tokens":900,"cache_creation_input_tokens":50,"output_tokens":3}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    events = await run(anthropic(body))
    usage = events[-1].usage
    assert (usage.prompt_tokens, usage.cache_read_tokens, usage.cache_creation_tokens) == (957, 900, 50)
    assert usage.completion_tokens == 3


async def test_anthropic_announces_the_counted_prompt_as_soon_as_the_stream_starts():
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        '{"input_tokens":5,"cache_read_input_tokens":900,"cache_creation_input_tokens":40}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"half"}}\n\n'
    )  # no message_stop: the connection closed
    seen = []
    with pytest.raises(ProviderError):
        async for event in anthropic(body).stream(MSGS, [], "m"):
            seen.append(event)
    first = seen[0]
    assert first.type == "partial_usage"
    usage = first.usage
    assert (usage.prompt_tokens, usage.cache_read_tokens, usage.cache_creation_tokens) == (945, 900, 40)


class Flaky:
    """Counts its prompt up front (a ``partial_usage``), then dies on the first call and answers on the second."""

    name, base_url = "flaky", "https://flaky.test"

    def __init__(self, dies: int = 1, text_before_dying: bool = True) -> None:
        self.calls, self.dies, self.text = 0, dies, text_before_dying

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        from k3code.providers.types import StreamEvent, Usage

        self.calls += 1
        yield StreamEvent(type="partial_usage", usage=Usage(prompt_tokens=700))
        if self.calls <= self.dies:
            if self.text:
                yield StreamEvent(type="text_delta", text="half")
            raise ProviderError("peer closed connection", status_code=502)
        usage = Usage(prompt_tokens=700, completion_tokens=5)
        yield StreamEvent(type="done", message=Message(role="assistant", content="whole", usage=usage), usage=usage)

    async def aclose(self):
        pass


def flaky_loop(tmp_path, provider):
    from k3code.agent.loop import AgentLoop
    from k3code.reliability.governor import Budget
    from k3code.router import Router, build_chain

    loop = AgentLoop(
        Router(build_chain([provider], [["m"]]), max_retries=3), system_prompt="t", cwd=tmp_path, permission_mode="yolo"
    )
    assert loop.reliability.governor is not None
    loop.reliability.governor.add_budget(Budget(scope="session", tokens=1_000_000))
    return loop


def spent(loop) -> int:
    return loop.reliability.governor._budgets["session"].used_tokens


async def test_the_loop_charges_the_prompt_of_an_attempt_that_died_and_not_twice_the_one_that_finished(tmp_path):
    provider = Flaky()
    loop = flaky_loop(tmp_path, provider)
    events = [e async for e in loop.run("go")]
    assert provider.calls == 2
    assert spent(loop) == 700 + 705, "the dead attempt's prompt, and the finished one once (not its provisional too)"
    assert not any(e.type == "partial_usage" for e in events), "the marker does not leave the loop"


async def test_an_attempt_that_died_before_any_text_is_charged_when_the_retry_starts(tmp_path):
    provider = Flaky(dies=2, text_before_dying=False)  # no output, so no reset event: the next attempt's marker arrives
    loop = flaky_loop(tmp_path, provider)
    [e async for e in loop.run("go")]
    assert provider.calls == 3 and spent(loop) == 700 + 700 + 705


async def test_an_attempt_that_died_is_charged_when_the_failover_provider_counts_nothing_up_front(tmp_path):
    from k3code.agent.loop import AgentLoop
    from k3code.providers.types import StreamEvent, Usage
    from k3code.reliability.governor import Budget
    from k3code.router import Router, build_chain

    class Plain:
        """An OpenAI-compatible server: usage only arrives with the final message."""

        name, base_url = "plain", "https://plain.test"

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            usage = Usage(prompt_tokens=700, completion_tokens=5)
            yield StreamEvent(type="done", message=Message(role="assistant", content="ok", usage=usage), usage=usage)

        async def aclose(self):
            pass

    dying = Flaky(dies=1, text_before_dying=False)
    loop = AgentLoop(
        Router(build_chain([dying, Plain()], [["m"], ["m"]]), max_retries=0),
        system_prompt="t",
        cwd=tmp_path,
        permission_mode="yolo",
    )
    loop.reliability.governor.add_budget(Budget(scope="session", tokens=1_000_000))
    [e async for e in loop.run("go")]
    assert dying.calls == 1 and spent(loop) == 700 + 705


async def test_a_stop_while_streaming_charges_the_prompt_the_provider_had_counted(tmp_path):
    from k3code.providers.types import StreamEvent, Usage

    class Slow:
        name, base_url = "slow", "https://slow.test"

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            yield StreamEvent(type="partial_usage", usage=Usage(prompt_tokens=900))
            for i in range(50):
                yield StreamEvent(type="text_delta", text=f"t{i}")

        async def aclose(self):
            pass

    from k3code.agent.loop import AgentLoop
    from k3code.reliability.governor import Budget
    from k3code.router import Router, build_chain

    loop = AgentLoop(Router(build_chain([Slow()], [["m"]]), max_retries=0), system_prompt="t", cwd=tmp_path)
    loop.reliability.governor.add_budget(Budget(scope="session", tokens=1_000_000))
    seen = 0

    async def on_delta(text: str) -> None:
        nonlocal seen
        seen += 1
        if seen == 3:
            loop.interrupt()  # /stop

    loop.on_text_delta = on_delta
    [e async for e in loop.run("go")]
    assert seen < 50 and spent(loop) == 900


async def test_the_model_pickers_save_key_says_where_keys_come_from_and_never_echoes_one(tmp_path, monkeypatch):
    from m1cmd_helpers import make_server, rpc

    server, _ = make_server(tmp_path, monkeypatch)
    fake_key = "".join(["sk", "-test-", "z" * 12])  # built at run time: not a secret, and nothing for a scanner to flag
    res = await rpc(server, "model.save_key", {"slug": "t", "api_key": fake_key})
    message = str(res["error"]["message"])
    assert "does not store API keys" in message and "NOPE" in message
    assert fake_key not in str(res)
    await server.close()


async def test_a_call_a_server_reports_no_usage_for_still_counts_for_the_budget_guard_only(tmp_path):
    from k3code.agent.loop import AgentLoop
    from k3code.providers.types import Message as Msg
    from k3code.providers.types import StreamEvent, Usage
    from k3code.reliability.governor import Budget
    from k3code.router import Router, build_chain

    class Silent:
        name, base_url = "silent", "https://silent.test"

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            reply = Msg(role="assistant", content="an answer " * 40)  # no usage anywhere
            yield StreamEvent(type="done", message=reply, usage=None)

        async def aclose(self):
            pass

    loop = AgentLoop(
        Router(build_chain([Silent()], [["m"]]), max_retries=0),
        system_prompt="sys " * 50,
        cwd=tmp_path,
        permission_mode="yolo",
    )
    loop.reliability.governor.add_budget(Budget(scope="session", tokens=1_000_000))
    events = [e async for e in loop.run("a question " * 20)]
    used = loop.reliability.governor._budgets["session"].used_tokens
    assert 100 < used < 2000, "the request and the reply, estimated"
    assert events[-1].message.usage is None, "what is shown and logged stays empty"

    class Reports(Silent):
        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            usage = Usage(prompt_tokens=10, completion_tokens=2)
            yield StreamEvent(type="done", message=Msg(role="assistant", content="ok", usage=usage), usage=usage)

    loop2 = AgentLoop(Router(build_chain([Reports()], [["m"]]), max_retries=0), system_prompt="s", cwd=tmp_path)
    loop2.reliability.governor.add_budget(Budget(scope="session", tokens=1_000_000))
    [e async for e in loop2.run("q")]
    assert loop2.reliability.governor._budgets["session"].used_tokens == 12, "a reported figure is never replaced"


async def test_the_one_hour_cache_ttl_is_priced_by_the_provider_that_asked_for_it():
    def provider(ttl: str, cache: str = "auto") -> AnthropicProvider:
        return AnthropicProvider(
            name="a",
            base_url="https://a.test",
            api_key="k",
            client=client(OK_ANTHROPIC),
            cache_ttl=ttl,
            prompt_cache=cache,
        )

    assert (await run(provider("1h")))[-1].usage.cache_write_multiple == 2.0
    assert (await run(provider("5m")))[-1].usage.cache_write_multiple is None
    assert (await run(provider("1h", cache="off")))[-1].usage.cache_write_multiple is None, "no cache, no surcharge"


COST_BODY = (
    'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
    'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"cost":%s}}\n\n'
    "data: [DONE]\n\n"
)


@pytest.mark.parametrize(
    ("base_url", "cost", "expected"),
    [
        ("https://openrouter.ai/api/v1", "0.0042", 0.0042),
        ("https://openrouter.ai/api/v1", "0", 0.0),  # a free model: nothing to pay, not a price-table guess
        ("https://my-vllm.internal/v1", "5.0", None),  # another server's `cost` is not assumed to be dollars
    ],
)
async def test_a_cost_field_is_dollars_only_from_openrouter(base_url, cost, expected):
    provider = OpenAICompatProvider(name="o", base_url=base_url, api_key="k", client=client(COST_BODY % cost))
    assert (await run(provider))[-1].usage.cost_usd == expected


async def test_the_router_names_the_model_a_partial_usage_belongs_to():
    from k3code.providers.types import StreamEvent, Usage
    from k3code.router import Router, build_chain

    class Counting:
        name, base_url = "p", "https://p.test"

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            yield StreamEvent(type="partial_usage", usage=Usage(prompt_tokens=700))
            done = Usage(prompt_tokens=700, completion_tokens=5)
            yield StreamEvent(type="done", message=Message(role="assistant", content="ok", usage=done), usage=done)

        async def aclose(self):
            pass

    router = Router(build_chain([Counting()], [["claude-opus-5-5"]]), max_retries=0)
    events = [e async for e in router.stream(MSGS, [], model="claude-opus-5-5")]
    partial = next(e for e in events if e.type == "partial_usage")
    assert partial.usage.model == "claude-opus-5-5", "an attempt that died is priced as the model it was sent to"
