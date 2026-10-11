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
