"""The optimizer's lifecycle and yardstick: an experiment whose window never fills expires instead of blocking every
later one, the A/B can judge cost in dollars (never guessing a price), the approvals-per-turn figure GOAL D2 reads
exists, and the weekly analysis runs on its own schedule."""

from __future__ import annotations

import statistics
from types import SimpleNamespace

import pytest

from k3code import confio
from k3code.learning import optimizer
from k3code.learning.hub import WEEK, LearningHub
from learn_helpers import FakeClock
from test_gateway import make_server

DAY = 86400.0
NO_LOG = SimpleNamespace(query=lambda *a, **k: [])


def _call(sid: str, i: int, model: str, tokens: int) -> dict:
    return {
        "ts": float(i),
        "session": sid,
        "kind": "call",
        "provider": "",
        "model": model,
        "detail": "",
        "tier": "cheap",
        "task_kind": "classification",
        "tokens_in": tokens,
        "tokens_out": 0,
        "turn": f"{sid}-{i}",
    }


def _sessions(prefix: str, n: int, model: str, tokens: int, prices: dict | None = None) -> list[dict]:
    rows = [_call(f"{prefix}{k}", i, model, tokens) for k in range(n) for i in range(4)]
    return list(optimizer.collect(rows, NO_LOG, [], since=0, prices=prices)["per_session"].values())


# --- expiry


def _started(tmp_path, clock, **kw):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("max_turns: 7\ntask_tiers:\n  title: fast\n")
    xp = optimizer.Experiments(tmp_path, clock)
    base = optimizer.collect([_call(f"b{k}", i, "m", 100) for k in range(12) for i in range(2)], NO_LOG, [], since=0)
    overlay = {"title": "Route classification to main", "patch": {"task_tiers": {"classification": "main"}}}
    xp.start(overlay, base, sessions=3, config_path=cfg, **kw)
    return xp, cfg, base


def test_an_experiment_past_its_age_expires_and_restores_the_old_value(tmp_path) -> None:
    clock = FakeClock()
    xp, cfg, _ = _started(tmp_path, clock)
    assert confio.read_yaml(cfg)["task_tiers"] == {"title": "fast", "classification": "main"}
    clock.advance(optimizer.EXPERIMENT_MAX_DAYS * DAY - 60)
    assert xp.expire_stale(cfg) == []  # not yet
    clock.advance(120)
    expired = xp.expire_stale(cfg)
    assert [x["id"] for x in expired] == ["x1"]
    assert xp.get("x1")["status"] == "expired" and xp.get("x1")["ended"] == clock()
    assert confio.read_yaml(cfg) == {"max_turns": 7, "task_tiers": {"title": "fast"}}
    assert "expired" in xp.format_status()


def test_a_stuck_experiment_no_longer_blocks_the_next_one(tmp_path) -> None:
    clock = FakeClock()
    xp, cfg, base = _started(tmp_path, clock)
    with pytest.raises(optimizer.ExperimentBusy):  # inside its window it still blocks
        xp.start({"title": "t", "patch": {"router": {"max_inline_wait": 40}}}, base, sessions=3, config_path=cfg)
    clock.advance(optimizer.EXPERIMENT_MAX_DAYS * DAY + 1)
    second = xp.start({"title": "t", "patch": {"router": {"max_inline_wait": 40}}}, base, sessions=3, config_path=cfg)
    assert second["id"] == "x2" and xp.get("x1")["status"] == "expired"
    assert "classification" not in confio.read_yaml(cfg)["task_tiers"]  # the first one's change is gone


def test_session_done_tells_the_user_an_experiment_expired(tmp_path) -> None:
    clock = FakeClock()
    xp, cfg, _ = _started(tmp_path, clock)
    clock.advance(optimizer.EXPERIMENT_MAX_DAYS * DAY + 1)
    notes: list[str] = []
    assert xp.session_done(lambda since: {}, notes.append, cfg) == []
    assert len(notes) == 1 and "expired" in notes[0] and "x1" in notes[0]


def test_the_age_is_configurable(tmp_path) -> None:
    clock = FakeClock()
    xp, cfg, _ = _started(tmp_path, clock)
    xp.max_age_days = 2
    clock.advance(2 * DAY + 1)
    assert [x["id"] for x in xp.expire_stale(cfg)] == ["x1"]


# --- cost in dollars


def test_call_cost_prefers_what_the_provider_reported_then_the_configured_price() -> None:
    prices = {"m": {"in": 10.0, "out": 20.0}, "cached": {"in": 10.0, "out": 20.0, "cache_read": 1.0}}
    row = {"model": "m", "tokens_in": 1_000_000, "tokens_out": 500_000}
    assert optimizer.call_cost({**row, "cost_usd": 0.5}, prices) == 0.5
    assert optimizer.call_cost(row, prices) == pytest.approx(20.0)  # 10 in + 10 out
    cached = {"model": "cached", "tokens_in": 1_000_000, "tokens_out": 0, "cache_read": 800_000}
    assert optimizer.call_cost(cached, prices) == pytest.approx(2.0 + 0.8)  # 200k at 10 + 800k at 1
    assert optimizer.call_cost({**row, "model": "unpriced"}, prices) is None
    assert optimizer.call_cost(row, None) is None
    assert optimizer.call_cost(row, {"m": {"in": 10.0}}) is None  # half a price is no price


def test_a_cheaper_model_that_uses_more_tokens_wins_in_dollars_and_loses_in_tokens() -> None:
    prices = {"big": {"in": 10.0, "out": 0.0}, "small": {"in": 1.0, "out": 0.0}}

    def mean_diff(prices_):
        control = _sessions("c", 3, "big", 1000, prices_)
        treatment = _sessions("t", 3, "small", 2000, prices_)  # twice the tokens at a tenth of the price
        t, c = optimizer.arm_scores(treatment, control)
        return statistics.mean(t) - statistics.mean(c)

    assert mean_diff(prices) > 0
    assert mean_diff(None) < 0  # unpriced: only the token count is known, and it doubled


def test_one_unpriced_model_keeps_the_whole_comparison_in_tokens() -> None:
    prices = {"big": {"in": 10.0, "out": 0.0}}  # nobody priced "small"
    control = _sessions("c", 3, "big", 1000, prices)
    treatment = _sessions("t", 3, "small", 2000, prices)
    assert optimizer.arms_priced(control, _sessions("p", 3, "big", 900, prices))
    assert not optimizer.arms_priced(treatment, control)
    t, c = optimizer.arm_scores(treatment, control)
    assert statistics.mean(t) < statistics.mean(c)  # judged by tokens, so the extra tokens count against it


def test_collect_reports_dollars_only_when_every_call_is_priced() -> None:
    rows = [_call("s", i, "big", 1000) for i in range(2)] + [_call("s", 2, "small", 1000)]
    priced = optimizer.collect(rows, NO_LOG, [], since=0, prices={"big": {"in": 10.0, "out": 0.0}})
    assert priced["cost_basis"] == "tokens" and priced["cost_usd"] is None  # "small" would look free
    both = optimizer.collect(
        rows, NO_LOG, [], since=0, prices={"big": {"in": 10.0, "out": 0.0}, "small": {"in": 1.0, "out": 0.0}}
    )
    assert both["cost_basis"] == "usd" and both["cost_usd"] == pytest.approx(0.021)
    assert both["cost_per_turn"] == pytest.approx(0.021 / 3)
    none = optimizer.collect(rows, NO_LOG, [], since=0)
    assert none["cost_basis"] == "tokens" and none["cost_usd"] is None


def test_the_score_compares_dollars_per_turn_when_both_sides_have_them() -> None:
    base = {"escalation_rate": 0.0, "loop_guard_per_session": 0.0, "approval_prompts_per_session": 0.0}
    old = {**base, "tokens_per_turn": 1000.0, "cost_per_turn": 0.01}
    cheaper = {**base, "tokens_per_turn": 2000.0, "cost_per_turn": 0.002}  # twice the tokens, a fifth of the cost
    assert optimizer.score(cheaper, old) > optimizer.score(old, old)
    no_cost = {**cheaper, "cost_per_turn": None}
    assert optimizer.score(no_cost, {**old, "cost_per_turn": None}) < optimizer.score(old, old)  # tokens: worse


def test_a_decided_experiment_records_its_cost_basis(tmp_path) -> None:
    clock = FakeClock()
    xp, cfg, _ = _started(tmp_path, clock)
    prices = {"big": {"in": 10.0, "out": 0.0}, "small": {"in": 1.0, "out": 0.0}}
    window = [_call(f"w{k}", i, "big" if k % 2 else "small", 1000) for k in range(40) for i in range(2)]
    metrics = optimizer.collect(window, NO_LOG, [], since=0, prices=prices)
    for _ in range(3):
        xp.session_done(lambda since: metrics, None, cfg)
    result = xp.get("x1").get("result") or {}
    assert result.get("cost_basis") in ("usd", "tokens")
    if "ci" in result:
        assert "cost in USD" in optimizer._describe(result) or result["cost_basis"] == "tokens"


# --- approvals per turn (GOAL D2)


def test_approvals_per_turn_is_measured() -> None:
    rows = [_call("s", i, "m", 100) for i in range(3)]
    rows += [{**rows[0], "kind": "approval", "ts": 10.0 + i} for i in range(6)]
    assert optimizer.collect(rows, NO_LOG, [], since=0)["approvals_per_turn"] == 2.0
    assert optimizer.collect([], NO_LOG, [], since=0)["approvals_per_turn"] is None


# --- the weekly analysis


def _hub(clock: FakeClock):
    server = make_server()
    hub = LearningHub(server, server.autonomy.proposals, clock)
    ran: list[int] = []

    async def run_optimizer(session=None):
        ran.append(1)
        return []

    hub.run_optimizer = run_optimizer  # type: ignore[method-assign]
    return server, hub, ran


async def test_the_weekly_analysis_runs_once_a_week_even_while_the_optimizer_is_off() -> None:
    clock = FakeClock()
    server, hub, ran = _hub(clock)
    assert hub.cfg["optimizer"]["enabled"] is False  # the default
    assert await hub.optimizer_tick() == 0 and len(ran) == 1
    assert await hub.optimizer_tick() is None and len(ran) == 1  # the same week: nothing
    clock.advance(WEEK - 60)
    assert await hub.optimizer_tick() is None and len(ran) == 1
    clock.advance(120)
    assert await hub.optimizer_tick() == 0 and len(ran) == 2
    await server.close()


async def test_a_restart_does_not_repeat_the_week() -> None:
    clock = FakeClock()
    server, hub, ran = _hub(clock)
    await hub.optimizer_tick()
    again = LearningHub(server, server.autonomy.proposals, clock)  # a new process reads the persisted timestamp
    again.run_optimizer = hub.run_optimizer  # type: ignore[method-assign]
    assert await again.optimizer_tick() is None and len(ran) == 1
    await server.close()


async def test_a_turns_maintenance_and_the_tick_share_one_run_of_the_week() -> None:
    clock = FakeClock()
    server, hub, ran = _hub(clock)
    hub._set_state(distilled=clock())  # the daily distill is not due: only the weekly part is
    assert (await hub.maintenance()).get("optimizer") == 0 and len(ran) == 1  # off by default, yet it ran
    assert await hub.optimizer_tick() is None and len(ran) == 1
    await server.close()


async def test_the_tick_does_nothing_while_learning_is_off() -> None:
    clock = FakeClock()
    server, hub, ran = _hub(clock)
    server.config.learning = {"enabled": False}
    assert await hub.optimizer_tick() is None and ran == []
    await server.close()


async def test_the_tick_expires_a_stale_experiment_and_says_so() -> None:
    clock = FakeClock()
    server, hub, _ = _hub(clock)
    cfg = hub.home / "config.yaml"
    cfg.write_text("max_turns: 7\n")
    base = optimizer.collect([_call(f"b{k}", i, "m", 100) for k in range(12) for i in range(2)], NO_LOG, [], since=0)
    patch = {"task_tiers": {"classification": "main"}}
    hub.experiments.start({"title": "t", "patch": patch}, base, sessions=3, config_path=cfg)
    notes: list[str] = []
    hub._notify = notes.append  # type: ignore[method-assign]
    clock.advance(hub.experiments.max_age_days * DAY + 1)
    await hub.optimizer_tick()
    assert hub.experiments.get("x1")["status"] == "expired" and len(notes) == 1 and "expired" in notes[0]
    await server.close()


def test_the_defaults_name_the_new_options() -> None:
    from k3code.learning import learning_cfg

    opt = learning_cfg(SimpleNamespace(learning={}))["optimizer"]
    assert opt["experiment_max_days"] == optimizer.EXPERIMENT_MAX_DAYS and opt["prices"] == {}
    assert opt["enabled"] is False  # unchanged: nothing is applied by itself until the user turns it on
