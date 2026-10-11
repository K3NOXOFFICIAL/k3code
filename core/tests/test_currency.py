"""Money is shown in EUR by default (USD on request): /stats, the budget messages, and caps given in EUR."""

from __future__ import annotations

import pytest

from k3code import pricing
from k3code.config import Settings
from k3code.reliability.governor import Budget
from k3code.reliability.hooks import build_reliability
from k3code.usage import UsageDB, format_stats


def test_the_default_currency_is_eur_and_usd_is_a_choice():
    cfg = Settings()
    assert (cfg.currency, cfg.eur_per_usd) == ("EUR", 0.86)
    assert pricing.money(cfg, 1.0) == "€0.8600" and pricing.money(cfg, None) == "unknown"
    usd = Settings(currency="usd")  # case does not matter
    assert usd.currency == "USD" and pricing.money(usd, 1.0) == "$1.0000" and pricing.per_usd(usd) == 1.0
    assert pricing.money(Settings(eur_per_usd=0.5), 3.0, digits=2) == "€1.50"


@pytest.mark.parametrize("bad", [{"currency": "GBP"}, {"eur_per_usd": 0}, {"eur_per_usd": -1}, {"eur_per_usd": True}])
def test_a_bad_currency_or_rate_is_refused_when_the_config_loads(bad):
    with pytest.raises(ValueError):
        Settings(**bad)


def test_stats_show_the_cost_in_the_configured_currency(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    db.record("call", session="s", model="m", tokens_in=10, tokens_out=1, cost_usd=2.0)
    rows = db.aggregate("session")
    assert rows[0]["cost_usd"] == 2.0  # stored and exported as USD
    assert "cost €1.7200" in format_stats(rows, "session", Settings())
    assert "cost €1.7200" in format_stats(rows, "session")  # no config: the default currency
    assert "cost $2.0000" in format_stats(rows, "session", Settings(currency="USD"))
    db.close()


def test_a_cap_given_in_eur_is_kept_in_usd_and_reported_in_eur():
    cfg = Settings(eur_per_usd=0.5, reliability={"session_eur": 1.0})
    rel = build_reliability(cfg, session="s")
    (budget,) = [b for b in rel.governor._budgets.values() if b.scope == "session"]
    assert budget.usd == pytest.approx(2.0)  # 1 EUR at 0.5 EUR per USD
    rel.governor.record_usage(0, 0, usd=2.5)
    exceeded = rel.governor.check_budgets()
    assert exceeded is not None and "EUR budget exceeded: 1.2500 > 1.0000" in exceeded.args[0]


def test_both_caps_given_the_lower_one_counts_and_usd_keeps_its_wording():
    cfg = Settings(eur_per_usd=0.5, reliability={"session_eur": 10.0, "session_usd": 3.0, "day_eur": 0.5})
    rel = build_reliability(cfg, session="s")
    caps = {b.scope: b.usd for b in rel.governor._budgets.values()}
    assert caps == {"session": pytest.approx(3.0), "day": pytest.approx(1.0)}
    usd = build_reliability(Settings(currency="USD", reliability={"session_usd": 1.0}), session="s2")
    usd.governor.record_usage(0, 0, usd=1.5)
    assert "session USD budget exceeded: 1.5000 > 1.0000" in usd.governor.check_budgets().args[0]
    assert Budget(scope="job", usd=1.0).would_exceed(usd=2.0) == "job USD budget exceeded: 2.0000 > 1.0000"
