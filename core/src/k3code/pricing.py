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
    ("fable", 10.0, 50.0),
    ("mythos", 10.0, 50.0),
    ("opus-5-5", 4.0, 20.0),
    # the Opus models before 4.5 cost three times as much as the ones after
    ("opus-4-1", 15.0, 75.0),
    ("opus-4-0", 15.0, 75.0),
    ("opus-4-2", 15.0, 75.0),
    ("claude-3-opus", 15.0, 75.0),
    ("opus", 5.0, 25.0),
    ("sonnet-5", 2.0, 10.0),
    ("sonnet", 3.0, 15.0),
    ("haiku-5", 0.10, 0.50),
    ("haiku", 1.0, 5.0),
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.5, 10.0),
    ("gpt-4.1-nano", 0.10, 0.40),
    ("gpt-4.1-mini", 0.40, 1.60),
    ("gpt-4.1", 2.0, 8.0),
)
CACHE_READ_MULTIPLE = 0.1
CACHE_WRITE_MULTIPLE = 1.25
#: Claude models whose cache read is cheaper than 10% of the input price (documented rates; the first match wins).
CLAUDE_CACHE_READ_MULTIPLES: tuple[tuple[str, float], ...] = (
    ("fable-5-1", 0.025),
    ("mythos-5-1", 0.025),
    ("opus-5-5", 0.05),
)
#: A prompt above this many tokens is billed at the second rate card of the models that have two.
LONG_PROMPT_TOKENS = 100_000
#: Haiku 5's rates for a prompt above LONG_PROMPT_TOKENS (input, output USD per million tokens).
HAIKU_5_LONG_PROMPT = (0.50, 2.50)
#: Names that are Claude models without "claude" in the id (the aliases of the claude-cli provider, the 5-family).
CLAUDE_NAMES = ("claude", "opus", "sonnet", "haiku", "fable", "mythos")
#: What a cached prompt token costs next to a fresh one, for the other families that price it (OpenAI's cached input is
#: a fraction of the input price and its writes are free); a model not listed pays the full input price for all of them.
CACHE_READ_MULTIPLES: tuple[tuple[str, float], ...] = (
    ("gpt-4o", 0.5),
    ("gpt-4.1", 0.25),
)


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
    dashed = name.replace(".", "-")  # gateways (OpenRouter, LiteLLM) write claude-opus-4.1 where Anthropic writes 4-1
    if dashed.endswith("opus-4"):  # "claude-opus-4" on its own is Opus 4.0; the needles below need a minor version
        return 15.0, 75.0
    for needle, price_in, price_out in PRICES_PER_MTOK:
        if needle in name or needle in dashed:
            return price_in, price_out
    return None


def _configured_price(config: Any, model: str) -> bool:
    """Whether the config names a price for ``model`` itself (then no built-in rate card is second-guessed)."""
    entry = ((getattr(config, "models", None) or {}).get(model) or {}).get("price")
    return isinstance(entry, dict)


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
    model = str(getattr(usage, "model", "") or "")
    name = model.lower()
    dashed = name.replace(".", "-")
    if "haiku-5" in dashed and prompt > LONG_PROMPT_TOKENS and not _configured_price(config, model):
        price_in, price_out = HAIKU_5_LONG_PROMPT
    if any(n in name for n in CLAUDE_NAMES):
        read_x = next((x for needle, x in CLAUDE_CACHE_READ_MULTIPLES if needle in dashed), CACHE_READ_MULTIPLE)
        write_x = getattr(usage, "cache_write_multiple", None) or CACHE_WRITE_MULTIPLE  # the 1-hour cache costs 2x
    else:  # no documented write surcharge; a read is discounted only for the families that say so
        read_x = next((x for needle, x in CACHE_READ_MULTIPLES if needle in name), 1.0)
        write_x = 1.0
    fresh = max(0, prompt - read - written)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    return (
        fresh * price_in + read * price_in * read_x + written * price_in * write_x + completion * price_out
    ) / 1_000_000


def day_loader(usage_db: Any, config: Any) -> Any:
    """The ``DAY_LEDGER`` loader for ``usage_db``: (tokens, USD) the log already holds for a day, rows priced like live
    calls (the provider's cost, else the price table, else the governor's blended rate)."""
    from types import SimpleNamespace

    def price(row: dict[str, Any]) -> float | None:
        return estimate_usd(config, SimpleNamespace(cost_usd=None, **row))

    return lambda day: usage_db.day_spend(day, price)


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
