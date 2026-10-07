"""Governor tests: PSI parsing/admission, caps, disk guard, budgets."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from k3code.reliability.governor import (
    Budget,
    BudgetExceeded,
    DiskGuardFull,
    Governor,
    GovernorConfig,
    parse_pressure,
    read_psi,
)


@pytest.fixture
def psi_dir(tmp_path: Path) -> Path:
    return tmp_path / "pressure"


def _write_psi(psi_dir: Path, resource: str, text: str) -> None:
    psi_dir.mkdir(parents=True, exist_ok=True)
    (psi_dir / resource).write_text(text, encoding="utf-8")


def _gov(psi_dir: Path, tmp_path: Path, **overrides) -> Governor:
    cfg = GovernorConfig(psi_path=str(psi_dir), home=tmp_path / "k3home", **overrides)
    return Governor(cfg)


# ── PSI parsing ──


def test_parse_pressure_some_and_full():
    text = "some avg10=12.50 avg60=3.00 avg300=1.00 total=42\nfull avg10=0.50 avg60=0.10 avg300=0.00 total=7\n"
    out = parse_pressure(text)
    assert out["some_avg10"] == 12.50
    assert out["some_avg60"] == 3.00
    assert out["full_avg10"] == 0.50


def test_parse_pressure_skips_garbage():
    out = parse_pressure("nonsense line\nsome avg10=abc avg60=1.0\n\n")
    assert out == {"some_avg60": 1.0}


def test_read_psi_missing_returns_empty(tmp_path: Path):
    assert read_psi(str(tmp_path / "nope" / "io")) == {}


# ── PSI admission ──


def test_io_admitted_below_threshold(psi_dir: Path, tmp_path: Path):
    _write_psi(psi_dir, "io", "some avg10=5.00 avg60=1.00 avg300=0.50 total=1\n")
    g = _gov(psi_dir, tmp_path)
    assert g.io_pressure() == 5.00
    assert g.io_admission_ok()


def test_io_blocked_at_or_above_threshold(psi_dir: Path, tmp_path: Path):
    _write_psi(psi_dir, "io", "some avg10=42.00 avg60=9.00 avg300=2.00 total=9\n")
    g = _gov(psi_dir, tmp_path)
    assert not g.io_admission_ok()


def test_io_pressure_zero_when_psi_unavailable(tmp_path: Path):
    g = _gov(tmp_path / "missing", tmp_path)  # psi_path dir does not exist
    assert g.io_pressure() == 0.0
    assert g.io_admission_ok()


async def test_slot_io_waits_for_psi_to_drop(psi_dir: Path, tmp_path: Path, monkeypatch):
    """An IO slot blocks while PSI is high, then admits once it falls."""
    _write_psi(psi_dir, "io", "some avg10=90.00 avg60=9.00 avg300=2.00 total=9\n")
    g = _gov(psi_dir, tmp_path)
    waits = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float):
        waits["n"] += 1
        if waits["n"] >= 2:
            _write_psi(psi_dir, "io", "some avg10=1.00 avg60=1.00 avg300=1.00 total=9\n")
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    async with g.slot("io"):
        assert g._counters.io == 1
    assert waits["n"] >= 2
    assert g._counters.io == 0


# ── caps ──


async def test_io_cap_limits_concurrent_io(psi_dir: Path, tmp_path: Path):
    _write_psi(psi_dir, "io", "some avg10=1.00 avg60=1.00 avg300=1.00 total=1\n")
    g = _gov(psi_dir, tmp_path, max_io_heavy=2, max_total=4)
    entered = 0
    entered_third = asyncio.Event()

    async def holder():
        nonlocal entered
        async with g.slot("io"):
            entered += 1
            await entered_third.wait()

    t1 = asyncio.create_task(holder())
    t2 = asyncio.create_task(holder())
    for _ in range(1000):
        await asyncio.sleep(0)
        if entered == 2:
            break
    assert entered == 2

    t3 = asyncio.create_task(holder())
    await asyncio.sleep(0.05)
    assert entered == 2  # third IO blocked by the cap

    entered_third.set()
    await asyncio.gather(t1, t2, t3)
    assert g._counters.io == 0
    assert g._counters.total == 0


async def test_total_cap_counts_cpu_and_io(psi_dir: Path, tmp_path: Path):
    _write_psi(psi_dir, "io", "some avg10=1.00 avg60=1.00 avg300=1.00 total=1\n")
    g = _gov(psi_dir, tmp_path, max_io_heavy=4, max_total=2)
    release = asyncio.Event()

    async def holder(kind: str):
        async with g.slot(kind):
            await release.wait()

    tasks = [asyncio.create_task(holder("cpu")) for _ in range(2)]
    await asyncio.sleep(0.05)
    assert g._counters.total == 2

    extra_done = asyncio.Event()

    async def extra():
        async with g.slot("cpu"):
            extra_done.set()

    t_extra = asyncio.create_task(extra())
    await asyncio.sleep(0.05)
    assert not extra_done.is_set()  # total cap blocks a third worker

    release.set()
    await asyncio.gather(*tasks, t_extra)
    assert extra_done.is_set()
    assert g._counters.total == 0


async def test_per_provider_llm_cap(psi_dir: Path, tmp_path: Path):
    _write_psi(psi_dir, "io", "some avg10=1.00 avg60=1.00 avg300=1.00 total=1\n")
    g = _gov(psi_dir, tmp_path, max_total=8, per_provider_streams=1)
    release = asyncio.Event()

    async def holder():
        async with g.slot("llm", provider="p1"):
            await release.wait()

    t1 = asyncio.create_task(holder())
    await asyncio.sleep(0.05)
    assert g._counters.by_provider.get("p1") == 1

    second_done = asyncio.Event()

    async def second():
        async with g.slot("llm", provider="p1"):
            second_done.set()

    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    assert not second_done.is_set()  # per-provider cap blocks

    release.set()
    await asyncio.gather(t1, t2)
    assert second_done.is_set()
    assert g._counters.by_provider.get("p1") == 0


# ── disk guard ──


def test_disk_guard_trips_below_floor(psi_dir: Path, tmp_path: Path, monkeypatch):
    class DU:
        total = 100
        used = 99
        free = 512  # bytes, way below the floor

    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda path: DU())
    g = _gov(psi_dir, tmp_path, min_free_bytes=2 * 1024**3)
    err = g.disk_stop()
    assert isinstance(err, DiskGuardFull)
    assert err.free_bytes == 512
    assert err.min_bytes == 2 * 1024**3


def test_disk_guard_passes_with_space(psi_dir: Path, tmp_path: Path, monkeypatch):
    class DU:
        total = 100 * 1024**3
        used = 10 * 1024**3
        free = 90 * 1024**3

    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda path: DU())
    g = _gov(psi_dir, tmp_path)
    assert g.disk_stop() is None


def test_disk_guard_stat_failure_does_not_block(psi_dir: Path, tmp_path: Path, monkeypatch):
    import shutil

    def boom(path):
        raise OSError("no stat")

    monkeypatch.setattr(shutil, "disk_usage", boom)
    g = _gov(psi_dir, tmp_path)
    assert g.disk_stop() is None


async def test_slot_raises_before_acquiring_when_disk_full(
    psi_dir: Path, tmp_path: Path, monkeypatch
):
    import shutil

    class DU:
        total = 10
        used = 10
        free = 0

    monkeypatch.setattr(shutil, "disk_usage", lambda path: DU())
    g = _gov(psi_dir, tmp_path)
    with pytest.raises(DiskGuardFull):
        async with g.slot("cpu"):
            pass
    assert g._counters.total == 0


# ── budgets ──


def test_token_budget_exceeded(psi_dir: Path, tmp_path: Path):
    g = _gov(psi_dir, tmp_path)
    g.add_budget(Budget(scope="session", tokens=100))
    assert g.check_budgets() is None
    g.record_usage(60, 30)  # 90 used
    assert g.check_budgets() is None
    err = g.apply_usage_and_check(5, 10)  # 105 > 100
    assert isinstance(err, BudgetExceeded)
    assert err.scope == "session"
    assert err.kind == "tokens"


def test_usd_budget_uses_default_price(psi_dir: Path, tmp_path: Path):
    g = _gov(psi_dir, tmp_path)  # tokens_per_usd = 2M
    g.add_budget(Budget(scope="day", usd=0.001))
    g.record_usage(1000, 1000)  # 2000 tokens = $0.001 -> not over (strict >)
    assert g.check_budgets() is None
    g.record_usage(1, 0)
    err = g.check_budgets()
    assert isinstance(err, BudgetExceeded)
    assert err.kind == "usd"


def test_explicit_usd_cost(psi_dir: Path, tmp_path: Path):
    g = _gov(psi_dir, tmp_path)
    g.add_budget(Budget(scope="job", usd=1.0))
    g.record_usage(10, 10, usd=1.5)
    err = g.check_budgets()
    assert isinstance(err, BudgetExceeded)
    assert "job" in str(err)


def test_budget_boundary_is_strictly_greater(psi_dir: Path, tmp_path: Path):
    b = Budget(scope="session", tokens=10)
    assert b.would_exceed(tokens=10) is None  # equal is fine
    assert b.would_exceed(tokens=11) is not None


# ── regressions: the Reliability wrapper and the day budget ──


def test_reliability_budget_exceeded_reports_instead_of_raising_typeerror():
    """emit(kind=...) collided with EventEmitter.emit's own `kind` parameter: every exceeded budget was a TypeError."""
    from k3code.reliability import Reliability
    from k3code.reliability import events as ev

    rel = Reliability.from_settings(None, session="s")
    rel.governor.add_budget(Budget(scope="session", tokens=100))
    seen = []
    rel.events.add(seen.append)
    rel.governor.record_usage(80, 40)
    err = rel.check_budgets()
    assert err is not None and err.scope == "session" and err.kind == "tokens"
    assert [e.kind for e in seen] == [ev.BUDGET_EXCEEDED]
    assert seen[0].data["budget_kind"] == "tokens"


def test_day_budget_is_shared_across_sessions_and_rolls_over_at_midnight():
    from k3code.reliability.governor import _DayLedger

    ledger = _DayLedger()  # what sessions of one daemon share (hooks.py passes DAY_LEDGER)
    a, b = Governor(day_ledger=ledger), Governor(day_ledger=ledger)
    day = ["2026-10-07"]
    a.today = b.today = lambda: day[0]
    a.add_budget(Budget(scope="day", tokens=100))
    b.add_budget(Budget(scope="day", tokens=100))
    a.record_usage(40, 20)
    assert a.check_budgets() is None and b.check_budgets() is None
    b.record_usage(30, 20)  # a different session spends the rest of today's budget
    assert a.check_budgets() is not None and b.check_budgets() is not None
    day[0] = "2026-10-08"  # next local day: counters start over
    assert a.check_budgets() is None and b.check_budgets() is None


def test_sessions_built_by_reliability_share_the_process_day_ledger():
    from k3code.reliability import Reliability
    from k3code.reliability.governor import DAY_LEDGER

    a, b = Reliability.from_settings(None, session="a"), Reliability.from_settings(None, session="b")
    assert a.governor.day_ledger is b.governor.day_ledger is DAY_LEDGER
