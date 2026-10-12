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
        ("claude-opus-4-20250514", (15.0, 75.0)),
        ("anthropic/claude-opus-4.1", (15.0, 75.0)),  # the gateways' dotted spelling
        ("anthropic/claude-opus-4", (15.0, 75.0)),
        ("anthropic/claude-opus-5.5", (4.0, 20.0)),
        ("anthropic/claude-opus-4.5", (5.0, 25.0)),
        ("claude-3-opus-20240229", (15.0, 75.0)),
        ("claude-sonnet-5-5", (2.0, 10.0)),
        ("claude-sonnet-4-6", (3.0, 15.0)),
        ("sonnet", (3.0, 15.0)),
        ("claude-haiku-5-5", (0.10, 0.50)),
        ("claude-haiku-4-5", (1.0, 5.0)),
        ("anthropic/claude-opus-5-5", (4.0, 20.0)),
        ("gpt-4o-mini", (0.15, 0.60)),
        ("gpt-4.1-mini", (0.40, 1.60)),
        ("gpt-4.1-nano", (0.10, 0.40)),
        ("gpt-4.1", (2.0, 8.0)),
        ("openai/gpt-4o", (2.5, 10.0)),
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


@pytest.mark.parametrize(
    ("model", "read_multiple"),
    [
        ("claude-fable-5-1", 0.025),
        ("claude-mythos-5-1", 0.025),
        ("anthropic/claude-fable-5.1", 0.025),
        ("claude-opus-5-5", 0.05),
        ("claude-fable-5", 0.1),  # only the 5.1 refresh reads that cheaply
        ("claude-sonnet-5-5", 0.1),
        ("claude-opus-4-6", 0.1),
    ],
)
def test_claude_cache_reads_cost_what_the_model_documents(model, read_multiple):
    usage = Usage(prompt_tokens=1_000_000, cache_read_tokens=1_000_000, model=model)
    price_in = model_price(Settings(), model)[0]
    assert estimate_usd(Settings(), usage) == pytest.approx(price_in * read_multiple)


def test_haiku_5_costs_more_above_a_prompt_of_100k_tokens():
    cfg = Settings()
    short = estimate_usd(cfg, Usage(prompt_tokens=90_000, completion_tokens=1_000, model="claude-haiku-5-5"))
    long = estimate_usd(cfg, Usage(prompt_tokens=300_000, completion_tokens=1_000, model="claude-haiku-5-5"))
    assert short == pytest.approx(0.09 * 0.10 + 0.001 * 0.50)
    assert long == pytest.approx(0.3 * 0.50 + 0.001 * 2.50)
    # a price the user configured is not second-guessed
    own = Settings(models={"claude-haiku-5-5": {"price": {"input": 0.2, "output": 1.0}}})
    assert estimate_usd(own, Usage(prompt_tokens=300_000, model="claude-haiku-5-5")) == pytest.approx(0.3 * 0.2)


def test_every_front_end_prices_the_usd_budgets_from_the_table(tmp_path, monkeypatch):
    from k3code.cli import _build_reliability

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    rel = _build_reliability(Settings(), "s-price")
    usage = Usage(prompt_tokens=1_000_000, model="claude-opus-5-5")
    assert rel.price_of is not None and rel.price_of(usage) == pytest.approx(4.0)
