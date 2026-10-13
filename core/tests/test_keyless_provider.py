"""#107: fast setup offers "local (Ollama, no key)", but an empty key failed every turn and doctor's api-keys check.

An entry with an empty ``api_key_env`` declares an endpoint that takes no key: it sends no key header and doctor does
not ask for one. An entry that names a variable still needs it, also on localhost (an OmniRoute gateway there does).
"""

from __future__ import annotations

import httpx
import pytest

from k3code.config import ProviderEntry, Settings
from k3code.providers import make_providers
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.base import ProviderError, request_headers
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message

OLLAMA = "http://localhost:11434/v1"
MSGS = [Message(role="user", content="hi")]


def test_a_keyless_entry_gets_no_key_header() -> None:
    assert request_headers("openai", "", keyless=True) == {"content-type": "application/json"}
    assert "x-api-key" not in request_headers("anthropic", "", keyless=True)
    with pytest.raises(ProviderError, match="No API key configured"):
        request_headers("openai", "")


def _recording_client(seen: list[httpx.Request], body: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_an_openai_turn_against_a_keyless_ollama_runs_without_a_key_header() -> None:
    seen: list[httpx.Request] = []
    body = 'data: {"choices":[{"delta":{"content":"pong"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    client = _recording_client(seen, body)
    provider = OpenAICompatProvider(name="local", base_url=OLLAMA, api_key="", keyless=True, client=client)
    events = [e async for e in provider.stream(MSGS, [], "llama3")]
    assert events[-1].message.content == "pong"
    assert "authorization" not in seen[0].headers


async def test_an_anthropic_turn_against_a_keyless_endpoint_runs_without_a_key_header() -> None:
    seen: list[httpx.Request] = []
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n'
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"pong"}}\n\n'
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":1}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    client = _recording_client(seen, body)
    provider = AnthropicProvider(name="local", base_url=OLLAMA, api_key="", keyless=True, client=client)
    events = [e async for e in provider.stream(MSGS, [], "m")]
    assert events[-1].message.content == "pong"
    assert "x-api-key" not in seen[0].headers


def _entry(api_key_env: str, **kw) -> ProviderEntry:
    return ProviderEntry(name="local", kind="openai", base_url=OLLAMA, api_key_env=api_key_env, **kw)


async def test_only_an_empty_api_key_env_makes_an_entry_keyless(monkeypatch) -> None:
    monkeypatch.delenv("K3CODE_FAKE_PROVIDER", raising=False)
    keyless, named = make_providers([_entry(""), _entry("LOCAL_API_KEY")])
    assert (keyless.keyless, named.keyless) == (True, False)
    # a variable that is named but empty is still the clear error, on localhost too (an OmniRoute gateway needs it)
    with pytest.raises(ProviderError, match="No API key configured"):
        [e async for e in named.stream(MSGS, [], "llama3")]
    for p in (keyless, named):
        await p.aclose()


def test_doctor_does_not_ask_a_keyless_entry_for_a_key(monkeypatch) -> None:
    from k3code.doctor import OK, WARN, check_keys

    monkeypatch.delenv("SOME_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("K3CODE_FAKE_PROVIDER", raising=False)
    chk = check_keys(Settings(providers=[_entry("")]))
    assert (chk.status, chk.detail) == (OK, "no provider needs a key")
    remote = ProviderEntry(name="gw", kind="openai", base_url="http://gw", api_key_env="SOME_PROVIDER_KEY")
    chk = check_keys(Settings(providers=[_entry(""), remote]))  # the keyless entry runs: a warning, not a failure
    assert chk.status == WARN and chk.detail == "missing env vars: SOME_PROVIDER_KEY"


async def test_doctor_lists_a_keyless_entrys_models_instead_of_warning_about_its_key(monkeypatch) -> None:
    from k3code import doctor

    async def reachable(base_url, timeout=5.0):
        return True, 3.0, "HTTP 404"

    async def models(p):
        return True, 3.0, "HTTP 200, 1 models"

    monkeypatch.setattr(doctor, "_probe_provider", reachable)
    monkeypatch.setattr(doctor, "_probe_key", models)
    checks = await doctor.check_providers(Settings(providers=[_entry("")]))
    row = next(c for c in checks if c.name == "provider:local")
    assert row.status == doctor.OK and row.detail.startswith("HTTP 200, 1 models")
