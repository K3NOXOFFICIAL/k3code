"""The day's caps count what the usage log already holds for today, so a restart (or a new `-p` process) does not hand
out the cap again."""

from __future__ import annotations

import time

import pytest

from k3code.config import Settings
from k3code.pricing import day_loader
from k3code.reliability.governor import Budget, Governor, _DayLedger
from k3code.usage import UsageDB


def today() -> str:
    return time.strftime("%Y-%m-%d")


def seeded_db(tmp_path) -> UsageDB:
    db = UsageDB(tmp_path / "usage.db")
    db.record("call", model="claude-opus-5-5", tokens_in=400_000, tokens_out=50_000)  # priced by the table
    db.record("call", model="local-model", tokens_in=1_000_000, tokens_out=0, cost_usd=0.30)  # the provider's figure
    db.record("subagent", model="claude-sonnet-5-5", tokens_in=100_000, tokens_out=10_000)
    db.record("tool", detail="read")  # not a model call
    db.record("call", ts=time.time() - 3 * 86400, model="claude-opus-5-5", tokens_in=9_000_000)  # another day
    return db


def test_the_log_totals_for_a_day_price_each_row_like_a_live_call(tmp_path):
    tokens, usd = day_loader(seeded_db(tmp_path), Settings())(today())
    assert tokens == 450_000 + 1_000_000 + 110_000
    opus = 400_000 * 4.0 / 1e6 + 50_000 * 20.0 / 1e6  # Opus 5.5: $4 in, $20 out
    sonnet = 100_000 * 2.0 / 1e6 + 10_000 * 10.0 / 1e6
    assert usd == pytest.approx(opus + 0.30 + sonnet)


def test_a_new_process_starts_its_day_from_the_log(tmp_path):
    ledger = _DayLedger()
    ledger.loader = day_loader(seeded_db(tmp_path), Settings())
    gov = Governor(day_ledger=ledger)
    gov.add_budget(Budget(scope="day", tokens=1_500_000))
    err = gov.check_budgets()
    assert err is not None and err.scope == "day", "1.56M tokens already spent today: the cap holds after the restart"
    assert Governor(day_ledger=_DayLedger()).check_budgets() is None


def test_the_ledger_rolls_to_a_new_day_from_that_days_rows(tmp_path):
    ledger = _DayLedger()
    ledger.loader = day_loader(seeded_db(tmp_path), Settings())
    ledger.roll(today())
    assert ledger.tokens == 1_560_000
    ledger.roll("2099-01-01")
    assert (ledger.tokens, ledger.usd) == (0, 0.0)


def test_an_unreadable_log_counts_from_zero_as_before():
    ledger = _DayLedger()

    def broken(day: str):
        raise OSError("disk gone")

    ledger.loader = broken
    ledger.roll(today())
    assert (ledger.tokens, ledger.usd) == (0, 0.0)


def test_cached_prompt_tokens_and_only_model_call_rows_count_in_the_log_totals(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    # one fully cached Opus 5.5 prompt of 1M tokens: a read costs 5% of the $4 input price
    db.record("call", model="claude-opus-5-5", tokens_in=1_000_000, cache_read=1_000_000)
    db.record("tool", model="claude-opus-5-5", tokens_in=500_000, tokens_out=500_000)  # not a model call
    tokens, usd = day_loader(db, Settings())(today())
    assert tokens == 1_000_000 and usd == pytest.approx(4.0 * 0.05)
    # and a cache write is billed at 125% of the input price
    db.record("call", model="claude-sonnet-5-5", tokens_in=1_000_000, cache_write=1_000_000)
    _, usd = day_loader(db, Settings())(today())
    assert usd == pytest.approx(4.0 * 0.05 + 2.0 * 1.25)


def test_a_subagent_row_that_adds_up_many_calls_is_not_priced_as_one_long_prompt(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    # ten 15k-token requests of a Haiku-5 sub-agent: each is far below the long-prompt card, whatever they add up to
    db.record("subagent", model="test-haiku-5", tokens_in=150_000, tokens_out=5_000)
    _, usd = day_loader(db, Settings())(today())
    assert usd == pytest.approx(150_000 * 0.10 / 1e6 + 5_000 * 0.50 / 1e6)
    # a single call of that size is one long request, and keeps the card (0.50 in, 2.50 out)
    db.record("call", model="test-haiku-5", tokens_in=150_000, tokens_out=5_000)
    _, usd = day_loader(db, Settings())(today())
    assert usd == pytest.approx(150_000 * 0.10 / 1e6 + 5_000 * 0.50 / 1e6 + 150_000 * 0.50 / 1e6 + 5_000 * 2.50 / 1e6)


def test_a_governor_with_an_empty_ledger_and_a_day_budget_is_quiet():
    gov = Governor(day_ledger=_DayLedger())
    gov.add_budget(Budget(scope="day", tokens=1))
    assert gov.check_budgets() is None, "nothing counted: the control the restart tests are measured against"


def test_a_gateway_daemon_starts_its_day_from_the_usage_log(tmp_path, monkeypatch):
    """The wiring, not the ledger alone: a new GatewayServer reads what the log holds before it counts a token."""
    from k3code.reliability.governor import DAY_LEDGER
    from m1cmd_helpers import make_server

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    server.usage.record("call", model="claude-opus-5-5", tokens_in=1_500_000, tokens_out=50_000)
    gov = Governor(day_ledger=DAY_LEDGER)
    gov.add_budget(Budget(scope="day", tokens=1_000_000))
    err = gov.check_budgets()
    assert err is not None and err.scope == "day", "1.55M tokens are in today's log: the 1M cap holds after a restart"


def test_a_headless_run_starts_its_day_from_the_usage_log(tmp_path, monkeypatch):
    import asyncio

    from k3code import cli as cli_mod
    from k3code.config import ProviderEntry
    from k3code.permissions import PermissionMode
    from k3code.reliability.governor import DAY_LEDGER

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    seed = UsageDB(tmp_path / "home" / "usage.db")
    seed.record("call", model="claude-opus-5-5", tokens_in=1_500_000, tokens_out=50_000)
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    config = Settings(providers=[prov], default_model="default", reliability={"day_tokens": 1_000_000})
    result = asyncio.run(
        cli_mod._run_headless("go", model=None, permission_mode=PermissionMode.YOLO, config=config, json_output=True)
    )
    assert result is not None and result.get("error") == "budget_exceeded", result
    DAY_LEDGER.loader, DAY_LEDGER.date = None, ""  # leave the process-wide ledger as the other tests expect it
