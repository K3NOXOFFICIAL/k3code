"""A 200 response that dies mid-stream must fail, not become a successful (half-written) answer."""

from __future__ import annotations

import json

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
    ("error", "reason"),
    [
        ({"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"},
         FailoverReason.quota),
        ({"message": "Too many requests", "type": "requests", "code": "rate_limit_exceeded"},
         FailoverReason.rate_limit),
        ({"message": "Incorrect key provided", "type": "invalid_request_error", "code": "invalid_api_key"},
         FailoverReason.auth),
    ],
)
async def test_openai_in_band_string_code_classifies_by_the_code_not_as_server(error, reason):
    body = "data: " + json.dumps({"error": error}) + "\n\n"
    with pytest.raises(ProviderError) as exc:
        await run(openai(body))
    assert exc.value.status_code is None
    assert classify_api_error(exc.value, provider="o", model="m").reason == reason


async def test_openai_in_band_rate_limit_reads_the_retry_hint_from_the_message():
    error = {"message": "Rate limit reached. Please try again in 6s.", "code": "rate_limit_exceeded"}
    with pytest.raises(ProviderError) as exc:
        await run(openai("data: " + json.dumps({"error": error}) + "\n\n"))
    classified = classify_api_error(exc.value, provider="o", model="m")
    assert classified.reason == FailoverReason.rate_limit and classified.retry_after == 6.0


@pytest.mark.parametrize(
    ("etype", "status", "reason"),
    [
        ("billing_error", 402, FailoverReason.quota),
        ("permission_error", 403, FailoverReason.auth),
        ("authentication_error", 401, FailoverReason.auth),
        ("rate_limit_error", 429, FailoverReason.rate_limit),
    ],
)
async def test_anthropic_in_band_error_types_classify_by_type(etype, status, reason):
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
        f'event: error\ndata: {{"type":"error","error":{{"type":"{etype}","message":"x"}}}}\n\n'
    )
    with pytest.raises(ProviderError) as exc:
        await run(anthropic(body))
    assert exc.value.status_code == status
    assert classify_api_error(exc.value, provider="a", model="m").reason == reason


async def test_anthropic_in_band_unmapped_type_has_no_forced_status():
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
        'event: error\ndata: {"type":"error","error":{"type":"rate_limit_exceeded","message":"x"}}\n\n'
    )
    with pytest.raises(ProviderError) as exc:
        await run(anthropic(body))
    assert exc.value.status_code is None
    assert classify_api_error(exc.value, provider="a", model="m").reason == FailoverReason.rate_limit


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


def dropping_client(body: str, error: Exception) -> httpx.AsyncClient:
    """A 200 stream that sends ``body`` and then dies with a transport ``error`` (the connection drops mid-read)."""

    async def chunks():
        yield body.encode()
        raise error

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


OPENAI_FINISHED = (
    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'
)


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError("peer closed connection"), httpx.ReadError("reset")])
async def test_openai_drop_after_finish_reason_still_yields_the_whole_answer(error):
    provider = OpenAICompatProvider(
        name="o", base_url="https://o.test/v1", api_key="k", client=dropping_client(OPENAI_FINISHED, error)
    )
    events = await run(provider)
    done = [e for e in events if e.type == "done"]
    assert len(done) == 1 and done[0].message.content == "Hello world"
    assert done[0].message.stop_reason == "stop"


async def test_openai_drop_before_finish_reason_still_raises():
    body = 'data: {"choices":[{"delta":{"content":"Half a sent"}}]}\n\n'
    provider = OpenAICompatProvider(
        name="o",
        base_url="https://o.test/v1",
        api_key="k",
        client=dropping_client(body, httpx.RemoteProtocolError("peer closed connection")),
    )
    with pytest.raises(ProviderError) as exc:
        await run(provider)
    assert classify_api_error(exc.value, provider="o", model="m").reason == FailoverReason.server


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError("peer closed connection"), httpx.ReadError("reset")])
async def test_anthropic_drop_after_message_stop_still_yields_the_whole_answer(error):
    provider = AnthropicProvider(
        name="a", base_url="https://a.test", api_key="k", client=dropping_client(OK_ANTHROPIC, error)
    )
    events = await run(provider)
    done = [e for e in events if e.type == "done"]
    assert len(done) == 1 and done[0].message.content == "Hi"
    assert done[0].usage.completion_tokens == 1


async def test_anthropic_drop_before_message_stop_still_raises():
    body = OK_ANTHROPIC.replace('event: message_stop\ndata: {"type":"message_stop"}\n\n', "")
    provider = AnthropicProvider(
        name="a",
        base_url="https://a.test",
        api_key="k",
        client=dropping_client(body, httpx.RemoteProtocolError("peer closed connection")),
    )
    with pytest.raises(ProviderError) as exc:
        await run(provider)
    assert classify_api_error(exc.value, provider="a", model="m").reason == FailoverReason.server
