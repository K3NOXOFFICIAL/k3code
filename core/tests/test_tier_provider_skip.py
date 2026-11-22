"""A provider block opts out of a routing tier with an empty list: ``tiers: {strong: []}``."""

from __future__ import annotations

import logging

from k3code.config import ProviderEntry, Settings
from k3code.router import build_chain
from k3code.routing.tiers import Tier, tier_model_specs

LOCAL_MODEL = "Qwopus3.6-35B-A3B-Coder"


def _providers() -> list[ProviderEntry]:
    local = ProviderEntry(
        name="local",
        kind="openai",
        base_url="http://127.0.0.1:1234/v1",
        api_key_env="",
        models={"default": LOCAL_MODEL, "cheap": LOCAL_MODEL, "fast": LOCAL_MODEL},
        tiers={"strong": []},
    )
    claude_cli = ProviderEntry(
        name="claude-cli",
        kind="claude-cli",
        models={"default": "sonnet", "strong": "opus", "cheap": "haiku", "fast": "haiku"},
        tiers={"main": [], "cheap": [], "fast": []},
    )
    custom = ProviderEntry(
        name="custom",
        kind="openai",
        base_url="https://api.example/v1",
        api_key_env="K",
        models={
            "default": "auto/coding-manual",
            "strong": "auto/claude-opus",
            "cheap": "auto/claude-haiku",
            "fast": "auto/claude-sonnet",
        },
    )
    return [local, claude_cli, custom]


def _chain(settings: Settings, tier: Tier) -> list[tuple[str, str]]:
    return [(e.provider.name, e.model) for e in build_chain(settings.providers, tier_model_specs(settings, tier))]


def test_skipped_tiers_leave_the_provider_out_of_that_chain() -> None:
    settings = Settings(providers=_providers(), default_model="default")

    assert _chain(settings, Tier.MAIN) == [("local", LOCAL_MODEL), ("custom", "auto/coding-manual")]
    assert _chain(settings, Tier.CHEAP) == [("local", LOCAL_MODEL), ("custom", "auto/claude-haiku")]
    assert _chain(settings, Tier.FAST) == [("local", LOCAL_MODEL), ("custom", "auto/claude-sonnet")]
    assert _chain(settings, Tier.STRONG) == [("claude-cli", "opus"), ("custom", "auto/claude-opus")]


def test_a_non_empty_tier_list_is_still_a_model_list() -> None:
    provider = ProviderEntry(
        name="p",
        kind="openai",
        base_url="http://p",
        api_key_env="K",
        models={"default": "d"},
        tiers={"strong": ["a", "b"]},
    )
    settings = Settings(providers=[provider], default_model="default")

    assert tier_model_specs(settings, Tier.STRONG) == [["a", "b"]]


def test_a_tier_that_every_provider_skips_falls_back_to_the_full_list(caplog) -> None:
    only = ProviderEntry(
        name="only",
        kind="openai",
        base_url="http://o",
        api_key_env="K",
        models={"default": "d", "strong": "s"},
        tiers={"strong": []},
    )
    settings = Settings(providers=[only], default_model="default")

    with caplog.at_level(logging.WARNING, logger="k3code.routing.tiers"):
        specs = tier_model_specs(settings, Tier.STRONG)

    assert specs == ["s"]
    assert "every provider skips" in caplog.text
