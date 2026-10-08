"""Provider factory."""

from __future__ import annotations

from k3code.config import ProviderEntry
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.base import Provider
from k3code.providers.claude_cli import ClaudeCliProvider
from k3code.providers.fake import FakeProvider, fake_provider_requested, load_fake_steps
from k3code.providers.openai_compat import OpenAICompatProvider


def make_providers(entries: list[ProviderEntry]) -> list[Provider]:
    """Create provider instances from config entries."""
    # K3CODE_FAKE_PROVIDER=<script.json> replaces every real provider with a
    # scripted FakeProvider (one per configured entry, so the chain still
    # fails over). Used by tests and offline smoke runs.
    if script_path := fake_provider_requested():
        steps = load_fake_steps(script_path)
        return [FakeProvider(name=entry.name, steps=list(steps)) for entry in entries] or [
            FakeProvider(name="fake", steps=steps)
        ]

    providers: list[Provider] = []
    for entry in entries:
        if entry.kind == "openai":
            providers.append(
                OpenAICompatProvider(
                    name=entry.name,
                    base_url=entry.base_url,
                    api_key=entry.api_key,
                )
            )
        elif entry.kind == "anthropic":
            providers.append(
                AnthropicProvider(
                    name=entry.name,
                    base_url=entry.base_url,
                    api_key=entry.api_key,
                )
            )
        elif entry.kind == "claude-cli":
            providers.append(
                ClaudeCliProvider(
                    name=entry.name, thinking_tokens=entry.thinking_tokens, thinking_models=entry.thinking_models
                )
            )
    return providers


__all__ = [
    "make_providers",
    "Provider",
    "OpenAICompatProvider",
    "AnthropicProvider",
    "ClaudeCliProvider",
    "FakeProvider",
]
