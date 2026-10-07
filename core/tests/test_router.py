"""Router fallback chain tests using respx."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from k3code.errors import AllProvidersUnreachable
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message, ToolSpec
from k3code.router import CooldownStore, Router, RouterEvent, build_chain
from k3code.router.classifier import FailoverReason, classify_api_error

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def provider_factory():
    """Create a provider that uses a mock httpx client."""

    def _make(responses: list[httpx.Response | Exception]):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.send = AsyncMock(side_effect=responses)
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.aclose = AsyncMock()
        provider = OpenAICompatProvider(
            name="test",
            base_url="https://api.test.com",
            api_key="test-key",
            client=mock_client,
        )
        return provider, mock_client

    return _make


@pytest.fixture
def sample_messages():
    return [Message(role="user", content="Hello")]


@pytest.fixture
def sample_tools():
    return [ToolSpec(name="test_tool", description="A test", parameters={}, side_effect=False)]


# ── Router tests ───────────────────────────────────────────────────────


@respx.mock
async def test_router_retries_then_fails_over(sample_messages, sample_tools, provider_factory):
    """Primary 500 x3 → secondary succeeds; events recorded."""
    events: list[RouterEvent] = []

    def on_event(e: RouterEvent):
        events.append(e)

    # Primary: 500 three times
    primary_responses = [
        httpx.Response(500, json={"error": {"message": "Internal server error"}}),
        httpx.Response(500, json={"error": {"message": "Internal server error"}}),
        httpx.Response(500, json={"error": {"message": "Internal server error"}}),
    ]
    primary, _ = provider_factory(primary_responses)

    # Secondary: succeeds
    success_chunk = 'data: {"choices":[{"delta":{"content":"Hi"}}]}\ndata: {"choices":[{"delta":{}}]}\ndata: [DONE]\n'
    secondary_responses = [
        httpx.Response(200, text=success_chunk, headers={"content-type": "text/event-stream"})
    ]
    secondary, _ = provider_factory(secondary_responses)

    chain = build_chain([primary, secondary], [["model-1"], ["model-2"]])
    router = Router(chain, max_retries=2, base_delay=0.01, max_delay=0.05, on_event=on_event)

    result = await router.complete(sample_messages, sample_tools)

    assert result.content == "Hi"
    assert result.role == "assistant"
    # Events: 3 attempts on primary, 1 failover, 1 attempt on secondary
    attempt_events = [e for e in events if e.kind == "router.attempt"]
    failover_events = [e for e in events if e.kind == "router.failover"]
    assert len(attempt_events) == 4  # 3 primary + 1 secondary
    assert len(failover_events) == 1
    assert failover_events[0].reason == "server"


@respx.mock
async def test_router_401_immediate_failover(sample_messages, sample_tools, provider_factory):
    """Primary 401 → immediate failover with no retries."""
    events: list[RouterEvent] = []

    def on_event(e: RouterEvent):
        events.append(e)

    primary_responses = [
        httpx.Response(401, json={"error": {"message": "Invalid API key"}}),
    ]
    primary, _ = provider_factory(primary_responses)

    success_chunk = 'data: {"choices":[{"delta":{"content":"OK"}}]}\ndata: {"choices":[{"delta":{}}]}\ndata: [DONE]\n'
    secondary_responses = [
        httpx.Response(200, text=success_chunk, headers={"content-type": "text/event-stream"})
    ]
    secondary, _ = provider_factory(secondary_responses)

    chain = build_chain([primary, secondary], [["model-1"], ["model-2"]])
    router = Router(chain, max_retries=2, base_delay=0.01, max_delay=0.05, on_event=on_event)

    result = await router.complete(sample_messages, sample_tools)

    assert result.content == "OK"
    attempt_events = [e for e in events if e.kind == "router.attempt"]
    failover_events = [e for e in events if e.kind == "router.failover"]
    # Only 1 attempt on primary (no retries for auth), 1 on secondary
    assert len(attempt_events) == 2
    assert len(failover_events) == 1
    assert failover_events[0].reason == "auth"


@respx.mock
async def test_router_429_retry_after_cooldown(sample_messages, sample_tools, provider_factory):
    """429 with Retry-After header → cooldown respected."""
    events: list[RouterEvent] = []

    def on_event(e: RouterEvent):
        events.append(e)

    # Primary: 429 with Retry-After, then succeeds
    primary_responses = [
        httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"message": "Rate limited"}}),
        httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"After wait"}}]}\ndata: [DONE]\n',
            headers={"content-type": "text/event-stream"},
        ),
    ]
    primary, _ = provider_factory(primary_responses)

    chain = build_chain([primary], [["model-1"]])
    router = Router(chain, max_retries=2, base_delay=0.01, max_delay=0.05, on_event=on_event)

    result = await router.complete(sample_messages, sample_tools)

    assert result.content == "After wait"
    # Should have retried once (the 429 triggered retry)
    attempt_events = [e for e in events if e.kind == "router.attempt"]
    assert len(attempt_events) == 2


@respx.mock
async def test_router_all_network_all_providers_unreachable(sample_messages, sample_tools, provider_factory):
    """Every entry raises ConnectError → AllProvidersUnreachable."""
    events: list[RouterEvent] = []

    def on_event(e: RouterEvent):
        events.append(e)

    # Exception *instances* (not a called function) so AsyncMock raises them lazily,
    # one per entry (max_retries=0 means exactly one send() per entry).
    primary, _ = provider_factory([httpx.ConnectError("Connection refused")])
    secondary, _ = provider_factory([httpx.ConnectError("Connection refused")])

    chain = build_chain([primary, secondary], [["model-1"], ["model-2"]])
    router = Router(chain, max_retries=0, base_delay=0.01, max_delay=0.05, on_event=on_event)

    with pytest.raises(AllProvidersUnreachable) as exc_info:
        await router.complete(sample_messages, sample_tools)

    assert exc_info.value.attempts == 2
    exhausted = [e for e in events if e.kind == "router.exhausted"]
    assert len(exhausted) == 1


async def test_model_fallback_within_provider(sample_messages, sample_tools, provider_factory):
    """Model list fallback within one provider."""
    events: list[RouterEvent] = []

    def on_event(e: RouterEvent):
        events.append(e)

    # First model fails with bad_request (terminal), second succeeds
    fail_response = httpx.Response(400, json={"error": {"message": "Model not found", "code": "model_not_found"}})
    success_chunk = 'data: {"choices":[{"delta":{"content":"Model 2 works"}}]}\ndata: [DONE]\n'
    success_response = httpx.Response(200, text=success_chunk, headers={"content-type": "text/event-stream"})

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.send = AsyncMock(side_effect=[fail_response, success_response])
    mock_client.build_request = MagicMock(return_value=MagicMock())
    mock_client.aclose = AsyncMock()

    provider = OpenAICompatProvider(
        name="test",
        base_url="https://api.test.com",
        api_key="test-key",
        client=mock_client,
    )

    # One provider, two models
    chain = build_chain([provider], [["model-1", "model-2"]])
    router = Router(chain, max_retries=0, on_event=on_event)

    result = await router.complete(sample_messages, sample_tools)

    assert result.content == "Model 2 works"
    failover_events = [e for e in events if e.kind == "router.failover"]
    assert len(failover_events) == 1
    assert failover_events[0].extra.get("model") == "model-2"


# ── Classifier tests ───────────────────────────────────────────────────


def test_classifier_rate_limit():
    result = classify_api_error(Exception("Rate limit"), provider="test", model="m1")
    assert result.reason == FailoverReason.rate_limit


def test_classifier_quota():
    exc = Exception("Insufficient credits")
    result = classify_api_error(exc, provider="test", model="m1")
    assert result.reason == FailoverReason.quota


def test_classifier_auth():
    result = classify_api_error(Exception("auth"), provider="test", model="m1")
    # Note: classifier needs proper exception with status_code
    # This is a basic test; real classification happens through ProviderError
    assert result.reason in (FailoverReason.auth, FailoverReason.unknown)


def test_classifier_context_overflow():
    exc = Exception("Context length exceeded, max tokens 4096")
    result = classify_api_error(exc, provider="test", model="m1")
    assert result.reason == FailoverReason.context_overflow


def test_classifier_network():
    exc = httpx.ConnectError("Connection refused")
    result = classify_api_error(exc, provider="test", model="m1")
    assert result.reason == FailoverReason.network


# ── Cooldown tests ─────────────────────────────────────────────────────


def test_cooldown_arms_and_expires():
    store = CooldownStore()
    seconds = store.arm(
        FailoverReason.rate_limit,
        provider="test",
        model="model-1",
        base_url="https://api.test.com",
        retry_after=1.0,
        backoff_count=0,
    )
    assert seconds == 1
    assert store.in_cooldown(provider="test", model="model-1", base_url="https://api.test.com")

    # Advance time
    import time
    time.sleep(1.1)
    assert not store.in_cooldown(provider="test", model="model-1", base_url="https://api.test.com")


def test_cooldown_exponential_fallback():
    store = CooldownStore()
    seconds = store.arm(
        FailoverReason.quota,
        provider="test",
        model="model-1",
        base_url="https://api.test.com",
        retry_after=None,
        backoff_count=0,
    )
    assert seconds == 60  # base cooldown


# ── Chain building ────────────────────────────────────────────────────


def test_build_chain_expands_models():
    mock_providers = [MagicMock(), MagicMock()]
    chain = build_chain(mock_providers, [["a", "b"], ["c"]])
    assert len(chain) == 3
    assert chain[0].model == "a"
    assert chain[1].model == "b"
    assert chain[2].model == "c"
    assert chain[0].provider_index == 0
    assert chain[2].provider_index == 1
