"""Provider factory."""

from __future__ import annotations

from typing import Any

from k3code.config import ProviderEntry
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.base import Provider
from k3code.providers.claude_cli import ClaudeCliProvider
from k3code.providers.fake import FakeProvider, fake_provider_requested, load_fake_steps
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.stall import REASONING_TIMEOUT, is_local_endpoint


def provider_signature(entry: ProviderEntry) -> dict[str, Any]:
    """What of a config entry is baked into its provider instance (endpoint, key, claude-cli options).

    The model lists are left out: the routers read them from the config, so editing them never needs a new provider.
    A field added to ``ProviderEntry`` is part of the signature until it is excluded here.
    """
    return entry.model_dump(exclude={"models", "descriptions", "tiers"})


def stall_limits(entry: ProviderEntry) -> dict[str, float | None]:
    """The stall limits to build the entry's provider with: what the config says, else the reasoning-model limit for a
    server the user runs (None = the default for the model). A local server prefills a long prompt before its first
    token, and a stall retry would only repeat that prefill."""
    local = is_local_endpoint(entry.base_url, keyless=not entry.api_key_env)
    longer = REASONING_TIMEOUT if local else None
    return {
        "first_event_timeout": entry.first_event_timeout if entry.first_event_timeout is not None else longer,
        "idle_timeout": entry.idle_timeout if entry.idle_timeout is not None else longer,
    }


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
                    keyless=not entry.api_key_env,
                    prompt_cache=entry.prompt_cache,
                    cache_ttl=entry.cache_ttl,
                    **stall_limits(entry),
                )
            )
        elif entry.kind == "anthropic":
            providers.append(
                AnthropicProvider(
                    name=entry.name,
                    base_url=entry.base_url,
                    api_key=entry.api_key,
                    keyless=not entry.api_key_env,
                    prompt_cache=entry.prompt_cache,
                    cache_ttl=entry.cache_ttl,
                    **stall_limits(entry),
                )
            )
        elif entry.kind == "claude-cli":
            providers.append(
                ClaudeCliProvider(
                    name=entry.name,
                    thinking_tokens=entry.thinking_tokens,
                    thinking_models=entry.thinking_models,
                    effort=entry.effort,
                    persistent=entry.persistent,
                    max_sessions=entry.max_sessions,
                    idle_seconds=entry.idle_seconds,
                )
            )
    return providers


__all__ = [
    "make_providers",
    "provider_signature",
    "Provider",
    "OpenAICompatProvider",
    "AnthropicProvider",
    "ClaudeCliProvider",
    "FakeProvider",
]
