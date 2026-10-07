"""Provider factory."""

from __future__ import annotations

from k3code.config import ProviderEntry
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.base import Provider
from k3code.providers.openai_compat import OpenAICompatProvider


def make_providers(entries: list[ProviderEntry]) -> list[Provider]:
    """Create provider instances from config entries."""
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
    return providers


__all__ = ["make_providers", "Provider", "OpenAICompatProvider", "AnthropicProvider"]
