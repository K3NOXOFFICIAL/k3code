"""What a model call cost, for the USD budgets (``reliability.session_usd`` / ``day_usd``).

The provider's own figure wins (the claude-cli provider reports one). Otherwise the cost is estimated from list prices
per million tokens: ``models.<id>.price: {input: <usd>, output: <usd>}`` in the config, else the built-in table by model
family. The table is an estimate, not a bill; a model it does not know falls back to the governor's blended rate.
Claude's prompt cache is priced at its documented multiples: a read costs 10% of the input price and a write 125%.

Everything here is computed and stored in USD (the providers' costs and the price table are USD). What the user is
shown is in the configured ``currency`` (EUR by default): the USD figure times ``eur_per_usd``.
"""

from __future__ import annotations

from typing import Any

#: (substring of the lower-cased model id, input USD per million tokens, output USD per million tokens). The first
#: match wins, so a more specific name comes before the family it belongs to.
PRICES_PER_MTOK: tuple[tuple[str, float, float], ...] = (
    ("opus", 15.0, 75.0),
    ("sonnet", 3.0, 15.0),
    ("haiku", 1.0, 5.0),
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.5, 10.0),
    ("gpt-4.1-mini", 0.40, 1.60),
    ("gpt-4.1", 2.0, 8.0),
)
CACHE_READ_MULTIPLE = 0.1
CACHE_WRITE_MULTIPLE = 1.25


def model_price(config: Any, model: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens for ``model``: the config's entry, else the family table, else None."""
    if not model:
        return None
    entry = ((getattr(config, "models", None) or {}).get(model) or {}).get("price")
    if isinstance(entry, dict):
        try:
            return float(entry["input"]), float(entry["output"])
        except (KeyError, TypeError, ValueError):
            pass
    name = model.lower().rsplit("/", 1)[-1]
    for needle, price_in, price_out in PRICES_PER_MTOK:
        if needle in name:
            return price_in, price_out
    return None


def estimate_usd(config: Any, usage: Any) -> float | None:
    """The cost of one call in USD, or None when neither the provider nor a price says."""
    if usage is None:
        return None
    reported = getattr(usage, "cost_usd", None)
    if reported is not None:
        return float(reported)
    price = model_price(config, str(getattr(usage, "model", "") or ""))
    if price is None:
        return None
    price_in, price_out = price
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    read = int(getattr(usage, "cache_read_tokens", 0) or 0)
    written = int(getattr(usage, "cache_creation_tokens", 0) or 0)
    claude = any(n in str(getattr(usage, "model", "")).lower() for n in ("claude", "opus", "sonnet", "haiku"))
    if not claude:  # no documented cache discount for the other families: every prompt token at the input price
        read = written = 0
    fresh = max(0, prompt - read - written)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    return (
        fresh * price_in
        + read * price_in * CACHE_READ_MULTIPLE
        + written * price_in * CACHE_WRITE_MULTIPLE
        + completion * price_out
    ) / 1_000_000


#: EUR per USD when the config does not say: an approximation, the user sets ``eur_per_usd`` to today's rate.
DEFAULT_EUR_PER_USD = 0.86
CURRENCIES = ("EUR", "USD")
SYMBOLS = {"EUR": "€", "USD": "$"}


def currency(config: Any) -> str:
    """The currency money is shown in: ``EUR`` (the default) or ``USD``."""
    code = str(getattr(config, "currency", "") or "EUR").upper()
    return code if code in CURRENCIES else "EUR"


def eur_rate(config: Any) -> float:
    """EUR for one USD: the configured ``eur_per_usd``, else the default approximation."""
    rate = getattr(config, "eur_per_usd", None)
    ok = isinstance(rate, int | float) and not isinstance(rate, bool) and rate > 0
    return float(rate) if ok else DEFAULT_EUR_PER_USD


def per_usd(config: Any) -> float:
    """Units of the shown currency per one USD (1.0 for USD)."""
    return 1.0 if currency(config) == "USD" else eur_rate(config)


def convert(config: Any, usd: float) -> float:
    """``usd`` in the shown currency."""
    return usd * per_usd(config)


def eur_to_usd(config: Any, eur: float) -> float:
    """A cap given in EUR (``session_eur``, ``day_eur``) as the USD the budgets are kept in."""
    return eur / eur_rate(config)


def money(config: Any, usd: float | None, digits: int = 4) -> str:
    """``usd`` as the user sees it: ``€0.0123`` or ``$0.0123``; ``unknown`` for None."""
    if usd is None:
        return "unknown"
    return f"{SYMBOLS[currency(config)]}{convert(config, usd):.{digits}f}"
