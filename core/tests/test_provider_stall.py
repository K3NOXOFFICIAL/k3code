"""A model stream that makes no progress fails as a timeout; pings and SSE comments do not keep it alive (issue #126).

httpx's read timeout restarts on every byte, and both SSE loops skip every line that is not ``data:``, so an upstream
that sent only ``: keepalive`` comments or Anthropic ``ping`` events held a turn for as long as it liked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.base import Provider, ProviderError, to_provider_error
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.stall import StreamStalled, StreamWatchdog, is_stall
from k3code.providers.types import Message, StreamEvent
from k3code.router import Router, build_chain
from k3code.router.classifier import FailoverReason, classify_api_error

MSGS = [Message(role="user", content="hi")]
FAST = {"first_event_timeout": 0.15, "idle_timeout": 0.15}

COMMENT = b": OPENROUTER PROCESSING\n\n"
PING = b'event: ping\ndata: {"type": "ping"}\n\n'
OPENAI_ROLE_ONLY = b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
OPENAI_HI = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
OPENAI_THINKING = b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n'
OPENAI_END = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
ANTHROPIC_START = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
ANTHROPIC_HI = (
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"Hi"}}\n\n'
)
ANTHROPIC_END = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class Script(httpx.AsyncByteStream):
    """A response body played out over time: ``(seconds to wait, bytes)`` steps, then the connection closes.

    ``forever`` instead sends those bytes every 20 ms for good; ``hang`` leaves the connection open and silent.
    """

    def __init__(self, steps: list[tuple[float, bytes]], *, forever: bytes | None = None, hang: bool = False) -> None:
        self.steps = steps
        self.forever = forever
        self.hang = hang

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for wait, data in self.steps:
            await asyncio.sleep(wait)
            yield data
        while self.forever or self.hang:
            await asyncio.sleep(0.02 if self.forever else 3600)
            if self.forever:
                yield self.forever


def client(script: Script) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=script)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def openai(script: Script, **kwargs) -> OpenAICompatProvider:
    return OpenAICompatProvider(name="o", base_url="https://o.test/v1", api_key="k", client=client(script), **kwargs)


def anthropic(script: Script, **kwargs) -> AnthropicProvider:
    return AnthropicProvider(name="a", base_url="https://a.test", api_key="k", client=client(script), **kwargs)


async def run(provider: Provider, model: str = "m") -> list[StreamEvent]:
    # a missing watchdog must fail this test, not hang the suite
    return await asyncio.wait_for(_collect(provider, model), timeout=5)


async def _collect(provider: Provider, model: str) -> list[StreamEvent]:
    return [e async for e in provider.stream(MSGS, [], model)]


async def stalls(provider: Provider) -> ProviderError:
    with pytest.raises(ProviderError) as exc:
        await run(provider)
    assert classify_api_error(exc.value, provider="p", model="m").reason == FailoverReason.timeout
    assert is_stall(exc.value)
    return exc.value


# ── the stuck upstream ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "keepalive", [None, COMMENT, OPENAI_ROLE_ONLY], ids=["silence", "sse-comments", "empty-deltas"]
)
async def test_openai_stream_with_no_content_fails_as_a_timeout(keepalive):
    err = await stalls(openai(Script([], forever=keepalive, hang=True), **FAST))
    assert "no content" in err.message


@pytest.mark.parametrize("keepalive", [None, COMMENT, PING], ids=["silence", "sse-comments", "pings"])
async def test_anthropic_stream_with_no_content_fails_as_a_timeout(keepalive):
    err = await stalls(anthropic(Script([], forever=keepalive, hang=True), **FAST))
    assert "no content" in err.message


async def test_openai_stream_that_goes_quiet_after_some_content_fails_as_a_timeout():
    await stalls(openai(Script([(0, OPENAI_HI)], forever=COMMENT), **FAST))


async def test_anthropic_stream_that_goes_quiet_after_message_start_fails_as_a_timeout():
    await stalls(anthropic(Script([(0, ANTHROPIC_START)], forever=PING), **FAST))


async def test_a_server_that_never_answers_the_request_fails_as_a_timeout():
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await stalls(OpenAICompatProvider(name="o", base_url="https://o.test/v1", api_key="k", client=http, **FAST))
    await stalls(AnthropicProvider(name="a", base_url="https://a.test", api_key="k", client=http, **FAST))


@pytest.fixture
async def keepalive_server() -> AsyncIterator[int]:
    """A real socket that answers 200 and then sends an SSE comment every 50 ms for good."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        body = b": keepalive\n\n"
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\n\r\n")
            while True:
                writer.write(b"%x\r\n%s\r\n" % (len(body), body))
                await writer.drain()
                await asyncio.sleep(0.05)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_keepalives_reset_the_httpx_read_timeout_but_not_the_watchdog(keepalive_server, kind):
    # the transport's own timeout (0.3 s) never fires: a comment arrives every 50 ms
    http = httpx.AsyncClient(timeout=httpx.Timeout(connect=2, read=0.3, write=2, pool=2))
    base = f"http://127.0.0.1:{keepalive_server}"
    provider = (
        OpenAICompatProvider(name="o", base_url=f"{base}/v1", api_key="k", client=http, **FAST)
        if kind == "openai"
        else AnthropicProvider(name="a", base_url=base, api_key="k", client=http, **FAST)
    )
    try:
        await stalls(provider)
    finally:
        await http.aclose()


# ── a slow upstream that is alive is left alone ───────────────────────

# 0.8 s in all, twice the limit. Output comes at least every 0.2 s; a comment in between does not count as output.
ALIVE = {"first_event_timeout": 0.4, "idle_timeout": 0.4}
GAP = 0.1


async def test_openai_stream_that_keeps_producing_output_is_not_cut_off():
    steps = [(GAP, OPENAI_THINKING), (GAP, COMMENT), (GAP, OPENAI_THINKING), (GAP, COMMENT), (GAP, OPENAI_HI)]
    steps += [(GAP, COMMENT), (GAP, OPENAI_END)]
    events = await run(openai(Script(steps), **ALIVE))
    assert events[-1].type == "done" and events[-1].message.content == "Hi"


async def test_anthropic_stream_that_keeps_producing_output_is_not_cut_off():
    steps = [(GAP, ANTHROPIC_START), (GAP, PING), (GAP, ANTHROPIC_HI), (GAP, PING), (GAP, ANTHROPIC_HI)]
    steps += [(GAP, PING), (GAP, ANTHROPIC_END)]
    events = await run(anthropic(Script(steps), **ALIVE))
    assert events[-1].type == "done" and events[-1].message.content == "HiHi"


async def test_time_the_consumer_takes_is_not_the_upstreams_silence():
    # the whole reply is already there; the reader needs 0.3 s per event, twice the idle limit
    steps = [(0, OPENAI_THINKING), (0, OPENAI_THINKING), (0, OPENAI_HI), (0, OPENAI_END)]
    provider = openai(Script(steps), **FAST)
    seen = []
    async for event in provider.stream(MSGS, [], "m"):
        seen.append(event)
        await asyncio.sleep(0.3)
    assert seen[-1].type == "done"


# ── limits ─────────────────────────────────────────────────────────────


def limits(kind: str, model: str) -> tuple[float, float]:
    dog = StreamWatchdog.for_model(kind, model)
    return dog.first_event, dog.idle


def test_default_limits_follow_the_issue():
    assert limits("openai", "gpt-4o") == (60.0, 120.0)
    assert limits("anthropic", "claude-haiku-4-5-20251001") == (60.0, 120.0)


def test_a_model_that_reasons_before_it_answers_gets_longer_where_it_can_be_silent():
    # chat completions hide the reasoning of o-series and gpt-5 models: nothing arrives until it is done
    assert limits("openai", "gpt-5.6-terra-xhigh") == (300.0, 120.0)
    # an Anthropic stream starts at once (message_start), but a long think can show only pings
    assert limits("anthropic", "claude-opus-5-5") == (60.0, 300.0)


def test_explicit_limits_win():
    dog = StreamWatchdog.for_model("openai", "gpt-5", first_event=7, idle=9)
    assert (dog.first_event, dog.idle) == (7, 9)


# ── the router ─────────────────────────────────────────────────────────


class Stub(Provider):
    base_url = "https://stub.test"
    api_key = "k"

    def __init__(self, name: str, failure: BaseException | None) -> None:
        self.name = name
        self.failure = failure
        self.calls = 0

    async def stream(self, messages, tools, model, **kwargs) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.failure is not None:
            raise to_provider_error(self.failure, kind="openai")
        yield StreamEvent(type="done", message=Message(role="assistant", content="ok"))

    async def aclose(self) -> None:
        pass


async def no_sleep(seconds: float) -> None:
    pass


async def test_a_stalled_stream_is_retried_once_on_the_same_entry_then_fails_over():
    stuck = Stub("stuck", StreamStalled("stream stalled: no content from the model within 60s"))
    good = Stub("good", None)
    router = Router(build_chain([stuck, good], [["m"], ["m"]]), max_retries=2, sleep=no_sleep)
    result = await router.complete(MSGS, [])
    assert result.content == "ok"
    assert stuck.calls == 2  # one try and one retry, not the three a transient failure gets
    assert good.calls == 1


async def test_a_plain_timeout_keeps_the_full_retry_budget():
    slow = Stub("slow", httpx.ReadTimeout("read timed out"))
    good = Stub("good", None)
    router = Router(build_chain([slow, good], [["m"], ["m"]]), max_retries=2, sleep=no_sleep)
    await router.complete(MSGS, [])
    assert slow.calls == 3
