"""The built-in price table: current Claude generations, legacy Opus, cached input of the other families."""

from __future__ import annotations

import pytest

from k3code.config import Settings
from k3code.pricing import estimate_usd, model_price
from k3code.providers.types import Usage


@pytest.mark.parametrize(
    ("model", "price"),
    [
        ("claude-fable-5-1", (10.0, 50.0)),
        ("claude-mythos-5", (10.0, 50.0)),
        ("claude-opus-5-5", (4.0, 20.0)),
        ("claude-opus-5", (5.0, 25.0)),
        ("claude-opus-4-5", (5.0, 25.0)),
        ("claude-opus-4-1", (15.0, 75.0)),
        ("claude-3-opus-20240229", (15.0, 75.0)),
        ("claude-sonnet-5-5", (2.0, 10.0)),
        ("claude-sonnet-4-6", (3.0, 15.0)),
        ("sonnet", (3.0, 15.0)),
        ("claude-haiku-5-5", (0.10, 0.50)),
        ("claude-haiku-4-5", (1.0, 5.0)),
        ("anthropic/claude-opus-5-5", (4.0, 20.0)),
        ("gpt-4o-mini", (0.15, 0.60)),
        ("gpt-4.1-mini", (0.40, 1.60)),
        ("mystery", None),
    ],
)
def test_family_prices(model, price):
    assert model_price(Settings(), model) == price


def test_a_cached_prompt_is_discounted_for_the_families_that_price_it():
    cfg = Settings()
    gpt41 = estimate_usd(cfg, Usage(prompt_tokens=1_000_000, cache_read_tokens=800_000, model="gpt-4.1"))
    # gpt-4.1: $2 in; a cached token costs a quarter: 0.2M fresh + 0.8M * 0.25
    assert gpt41 == pytest.approx(2.0 * (0.2 + 0.8 * 0.25))


def test_other_families_pay_full_price_for_every_prompt_token():
    cfg = Settings(models={"local": {"price": {"input": 1.0, "output": 1.0}}})
    usage = Usage(prompt_tokens=1_000_000, cache_read_tokens=800_000, model="local")
    assert estimate_usd(cfg, usage) == pytest.approx(1.0)


def test_fable_cache_reads_are_discounted_like_claudes():
    usage = Usage(prompt_tokens=1_000_000, cache_read_tokens=1_000_000, model="claude-fable-5-1")
    assert estimate_usd(Settings(), usage) == pytest.approx(10.0 * 0.1)
