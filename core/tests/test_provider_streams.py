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


async def test_a_stream_that_dies_after_the_prompt_was_counted_carries_that_usage():
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        '{"input_tokens":5,"cache_read_input_tokens":900,"cache_creation_input_tokens":40}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"half"}}\n\n'
    )  # no message_stop: the connection closed
    with pytest.raises(ProviderError) as exc:
        await run(anthropic(body))
    usage = exc.value.usage
    assert (usage.prompt_tokens, usage.cache_read_tokens, usage.cache_creation_tokens) == (945, 900, 40)


async def test_the_loop_charges_the_tokens_of_an_attempt_that_died(tmp_path):
    from k3code.agent.loop import AgentLoop
    from k3code.providers.types import Message as Msg
    from k3code.providers.types import StreamEvent, Usage
    from k3code.reliability.governor import Budget
    from k3code.router import Router, build_chain

    class DiesOnce:
        name, base_url = "flaky", "https://flaky.test"

        def __init__(self):
            self.calls = 0

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type="text_delta", text="half")
                raise ProviderError("peer closed connection", status_code=502, usage=Usage(prompt_tokens=700))
            usage = Usage(prompt_tokens=700, completion_tokens=5)
            yield StreamEvent(type="done", message=Msg(role="assistant", content="whole", usage=usage), usage=usage)

        async def aclose(self):
            pass

    provider = DiesOnce()
    loop = AgentLoop(
        Router(build_chain([provider], [["m"]]), max_retries=2),
        system_prompt="t",
        cwd=tmp_path,
        permission_mode="yolo",
    )
    assert loop.reliability.governor is not None
    loop.reliability.governor.add_budget(Budget(scope="session", tokens=1_000_000))
    [e async for e in loop.run("go")]
    assert provider.calls == 2
    assert loop.reliability.governor._budgets["session"].used_tokens == 700 + 705, "the dead attempt and the good one"


async def test_the_model_pickers_save_key_says_where_keys_come_from_and_never_echoes_one(tmp_path, monkeypatch):
    from m1cmd_helpers import make_server, rpc

    server, _ = make_server(tmp_path, monkeypatch)
    fake_key = "".join(["sk", "-test-", "z" * 12])  # built at run time: not a secret, and nothing for a scanner to flag
    res = await rpc(server, "model.save_key", {"slug": "t", "api_key": fake_key})
    message = str(res["error"]["message"])
    assert "does not store API keys" in message and "NOPE" in message
    assert fake_key not in str(res)
    await server.close()
