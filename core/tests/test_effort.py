"""/effort and /reasoning reach the provider requests."""

from __future__ import annotations

import pytest

from k3code.providers import effort
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message


def _anthropic_payload(model: str) -> dict:
    p = AnthropicProvider(name="a", api_key="k")
    return p._payload([Message(role="user", content="hi")], [], model, max_tokens=10, temperature=None)


def _openai_payload(model: str) -> dict:
    p = OpenAICompatProvider(name="o", base_url="http://x", api_key="k")
    return p._payload([Message(role="user", content="hi")], [], model, max_tokens=10, temperature=None)


@pytest.mark.parametrize(
    ("model", "sent"),
    [
        ("claude-sonnet-5-5", True),
        ("claude-opus-5", True),
        ("claude-opus-4-8", True),
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", False),
        ("claude-3-5-sonnet-latest", False),
    ],
)
def test_anthropic_sends_effort_only_to_models_that_take_it(model: str, sent: bool) -> None:
    token = effort.REASONING_EFFORT.set("xhigh")
    try:
        payload = _anthropic_payload(model)
    finally:
        effort.REASONING_EFFORT.reset(token)
    assert ("output_config" in payload) is sent
    if sent:
        assert payload["output_config"] == {"effort": "xhigh"}
    assert "thinking" not in payload


def test_no_effort_by_default() -> None:
    assert "output_config" not in _anthropic_payload("claude-sonnet-5-5")
    assert "reasoning_effort" not in _openai_payload("gpt-5")


def test_openai_reasoning_effort_is_clamped() -> None:
    token = effort.REASONING_EFFORT.set("max")
    try:
        assert _openai_payload("openai/gpt-5")["reasoning_effort"] == "high"
        assert "reasoning_effort" not in _openai_payload("llama3.1")
    finally:
        effort.REASONING_EFFORT.reset(token)
