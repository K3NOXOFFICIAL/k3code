import itertools
import random
from types import SimpleNamespace

import pytest

from k3code import confio
from k3code.autonomy.proposals import ProposalStore
from k3code.config import Settings
from k3code.errors import ChainExhausted
from k3code.learning import optimizer
from k3code.learning.decisions import DecisionLog
from k3code.providers.types import Message
from k3code.routing.caller import ModelCaller
from k3code.routing.tiers import TaskKind
from k3code.usage import UsageDB
from learn_helpers import FakeClock


def ev(ts, kind, session="s1", tier="", task_kind="", detail=""):
    return {
        "ts": ts,
        "session": session,
        "kind": kind,
        "provider": "",
        "model": "",
        "detail": detail,
        "tier": tier,
        "task_kind": task_kind,
    }


def rows(t0, calls, escalations, session="s1", kind="classification", approvals=0):
    out = [ev(t0 + i, "call", session, "cheap", kind) for i in range(calls)]
    out += [ev(t0 + 50 + i, "escalated", session, "main", kind, "cheap->main: x") for i in range(escalations)]
    out += [ev(t0 + 80 + i, "approval", session) for i in range(approvals)]
    return out


def test_collect_metrics(tmp_path):
    log = DecisionLog(tmp_path)
    for c in ("accept", "dismiss", "dismiss"):
        log.record("proposal", choice=c, detail={"kind": "x"}, ts=105)
    scope = [
        {"ts": 100, "type": "verdict", "hash": "h1", "verdict": {"scope": "small"}},
        {"ts": 101, "type": "outcome", "hash": "h1", "outcome": "error"},
        {"ts": 102, "type": "verdict", "hash": "h2", "verdict": {"scope": "small"}},
        {"ts": 103, "type": "outcome", "hash": "h2", "outcome": "done"},
    ]
    m = optimizer.collect(rows(100, 10, 4, approvals=6), log, scope, since=100)
    assert m["escalation_rate"] == 0.4 and m["approval_prompts_per_session"] == 6
    assert m["tiers"]["cheap"]["rate"] == 0.4 and m["escalated_kinds"] == {"classification": 4}
    assert m["proposal_accept_rate"] == round(1 / 3, 3) and m["scope_accuracy"] == 0.5


def tiered(cheap_calls, main_calls, cheap_tok, main_tok, escalations, kind="classification", plan_tok=0):
    """Measured call rows per tier (with tokens), the cheap-tier escalations, and optional planning tokens."""
    out = []
    for i in range(cheap_calls):
        out.append({**ev(i, "call", "s1", "cheap", kind), "tokens_in": cheap_tok, "tokens_out": 0, "turn": f"c{i}"})
    for i in range(main_calls):
        out.append({**ev(100 + i, "call", "s1", "main", kind), "tokens_in": main_tok, "tokens_out": 0, "turn": f"m{i}"})
    out += [ev(200 + i, "escalated", "s1", "main", kind, "cheap->main: x") for i in range(escalations)]
    if plan_tok:
        out.append({**ev(300, "call", "s1", "strong", "plan"), "tokens_in": plan_tok, "tokens_out": 0, "turn": "p"})
    return out


def test_suggest_from_metrics(tmp_path):
    log = DecisionLog(tmp_path)
    m = optimizer.collect(tiered(10, 10, 500, 550, 5), log, [], since=0)
    (c,) = optimizer.suggest(m, Settings())
    assert c["patch"] == {"task_tiers": {"classification": "main"}} and c["evidence"]["count"] == 5
    assert c["evidence"]["token_increase_pct"] == 4.8 and c["evidence"]["quality_gain"] == 0.5
    store = ProposalStore(tmp_path)
    (p,) = optimizer.propose(m, Settings(), store)
    assert p.kind == "optimizer" and p.payload["overlay"]["evidence"]
    assert optimizer.propose(m, Settings(), store) == []


NO_LOG = SimpleNamespace(query=lambda *a, **k: [])
_sids = itertools.count()


def metrics_for(esc_rate, n=12):
    """``n`` fresh sessions (the A/B samples), each with 10 calls of which ``esc_rate`` escalated."""
    out = []
    for _ in range(n):
        out += rows(0, 10, int(10 * esc_rate), session=f"w{next(_sids)}")
    return optimizer.collect(out, NO_LOG, [], since=0)


def ab_window(xid, treatment_esc, control_esc, per_arm=12):
    """Sessions of one period split by ``optimizer.arm``: each arm gets ``per_arm`` sessions at its escalation rate."""
    out, counts = [], {"treatment": 0, "control": 0}
    while min(counts.values()) < per_arm:
        sid = f"w{next(_sids)}"
        side = optimizer.arm(xid, sid)
        if counts[side] >= per_arm:
            continue
        counts[side] += 1
        out += rows(0, 10, int(10 * (treatment_esc if side == "treatment" else control_esc)), session=sid)
    return optimizer.collect(out, NO_LOG, [], since=0)


def synthetic_session(rng, sid, esc_p=0.2):
    """One noisy session: 3-15 calls of 500-3000 tokens (two calls a turn), escalations, a loop guard, approvals."""
    calls = rng.randint(3, 15)
    out = [
        {**ev(i, "call", sid, "cheap", "classification"), "tokens_in": rng.randint(500, 3000), "tokens_out": 0}
        | {"turn": f"{sid}-{i // 2}"}
        for i in range(calls)
    ]
    out += [
        ev(50 + i, "escalated", sid, "main", "classification", "cheap->main: x")
        for i in range(calls)
        if rng.random() < esc_p
    ]
    if rng.random() < 0.1:
        out.append(ev(70, "loop_guard", sid))
    out += [ev(80 + i, "approval", sid) for i in range(rng.randint(0, 3))]
    return out


def synthetic_window(rng, n=20, esc_p=0.2):
    out = []
    for _ in range(n):
        out += synthetic_session(rng, f"w{next(_sids)}", esc_p)
    return optimizer.collect(out, NO_LOG, [], since=0)


def test_ab_worse_metrics_rolls_back_automatically(tmp_path):
    clock = FakeClock()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("max_turns: 7\ntask_tiers:\n  title: fast\n")
    xp = optimizer.Experiments(tmp_path, clock)
    overlay = {
        "title": "Route classification to main",
        "patch": {"task_tiers": {"classification": "main"}, "learning": {"rank_threshold": 0.3}},
    }
    exp = xp.start(overlay, metrics_for(0.1), sessions=3, config_path=cfg)
    data = confio.read_yaml(cfg)
    assert data["task_tiers"] == {"title": "fast", "classification": "main"}
    assert data["learning"]["rank_threshold"] == 0.3
    assert exp["status"] == "active"
    notes: list[str] = []
    worse = metrics_for(0.6)
    for _ in range(2):
        assert xp.session_done(lambda since: worse, notes.append, cfg) == []
    assert xp.get("x1")["status"] == "active" and not notes
    done = xp.session_done(lambda since: worse, notes.append, cfg)
    assert [x["id"] for x in done] == ["x1"]
    x = xp.get("x1")
    assert x["status"] == "rolled_back" and x["after_score"] < x["baseline_score"]
    assert "rolled back automatically" in notes[0]
    restored = confio.read_yaml(cfg)
    assert restored == {"max_turns": 7, "task_tiers": {"title": "fast"}}
    assert "rolled_back" in xp.format_status()


def test_ab_better_metrics_keeps_overlay(tmp_path):
    cfg = tmp_path / "config.yaml"
    xp = optimizer.Experiments(tmp_path, FakeClock())
    xp.start(
        {"title": "t", "patch": {"router": {"max_inline_wait": 40}}}, metrics_for(0.5), sessions=1, config_path=cfg
    )
    notes: list[str] = []
    xp.session_done(lambda since: metrics_for(0.1), notes.append, cfg)
    assert xp.get("x1")["status"] == "kept" and "kept" in notes[0]
    assert confio.read_yaml(cfg)["router"]["max_inline_wait"] == 40
    assert xp.rollback("x1", config_path=cfg) and "router" not in confio.read_yaml(cfg)  # manual rollback of a kept one


def test_prompt_overlay_ab_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    xp = optimizer.Experiments(tmp_path, FakeClock())
    xp.start(
        {"title": "p", "name": "no-repeat", "prompt": "Do not repeat failing calls."}, metrics_for(0.1), sessions=1
    )
    assert "Do not repeat failing calls." in optimizer.overlay_prompt()
    from k3code.prompting import build_system_prompt

    assert "Do not repeat failing calls." in build_system_prompt("base", cwd=tmp_path, config=Settings())
    # a concurrent A/B: sessions in the control arm are built without the overlay
    treated = next(f"s{i}" for i in range(100) if optimizer.arm("x1", f"s{i}") == "treatment")
    held_back = next(f"s{i}" for i in range(100) if optimizer.arm("x1", f"s{i}") == "control")
    assert "Do not repeat" in build_system_prompt("base", cwd=tmp_path, config=Settings(), session_id=treated)
    assert "Do not repeat" not in build_system_prompt("base", cwd=tmp_path, config=Settings(), session_id=held_back)
    xp.session_done(lambda since: ab_window("x1", treatment_esc=0.7, control_esc=0.1))
    assert xp.get("x1")["status"] == "rolled_back" and optimizer.overlay_prompt() == ""


def test_self_improve_stub_writes_draft(tmp_path):
    p = optimizer.write_issue_draft(tmp_path, "Faster retries!", "details", clock=lambda: 5)
    assert p.parent == tmp_path / ".k3code" / "self-improve" and p.name == "5-faster-retries.md"
    assert "details" in p.read_text()


def test_overlay_cannot_touch_secrets(tmp_path):
    import pytest

    from k3code.learning.updateconfig import PatchRejected

    xp = optimizer.Experiments(tmp_path, FakeClock())
    with pytest.raises(PatchRejected):
        xp.start(
            {"title": "x", "patch": {"mem0": {"api_key_env": "A"}}}, metrics_for(0.1), config_path=tmp_path / "c.yaml"
        )


def test_outages_and_loop_guards_are_not_read_as_quality_escalations(tmp_path):
    rows_ = [ev(1, "call", tier="cheap", task_kind="title")] * 1
    rows_ += [ev(2, "call", tier="cheap", task_kind="title") for _ in range(3)]
    rows_ += [ev(3, "call", tier="main", task_kind="title")]
    rows_ += [ev(4, "outage", tier="main", task_kind="title", detail="cheap->main: chain exhausted: down")]
    rows_ += [ev(5, "loop_guard", tier="main", detail="loop_guard")]
    rows_ += [ev(6, "loop_guard", tier="cheap", detail="loop_guard")]
    m = optimizer.collect(rows_, DecisionLog(tmp_path), [], since=0)
    assert m["escalation_rate"] == 0 and m["escalated_kinds"] == {}
    assert m["outage_rate"] == 0.2 and m["loop_guard_per_session"] == 2.0
    assert m["tiers"]["main"]["loop_guard"] == 1 and m["tiers"]["cheap"]["loop_guard"] == 1
    assert m["tiers"]["main"]["loop_guard_rate"] == 1.0


class _Router:
    def __init__(self, down: bool) -> None:
        self.down = down

    async def complete(self, messages, tools, *, max_tokens: int = 0):
        if self.down:
            raise ChainExhausted("every provider of the tier is unreachable", "server")
        return SimpleNamespace(content="ok", usage=None)


class _Routers:
    def get(self, tier):
        return _Router(down=tier.value == "cheap")


async def test_chain_exhausted_escalates_as_an_outage_row(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    caller = ModelCaller(lambda: _Routers(), Settings(), db)
    res = await caller.complete(TaskKind.CLASSIFICATION, [Message(role="user", content="x")], session_id="s1")
    assert res.tier.value == "main"
    assert sorted(r["kind"] for r in db.rows(0)) == ["call", "outage"]
    m = optimizer.collect(db.rows(0), DecisionLog(tmp_path), [], since=0)
    assert m["escalation_rate"] == 0 and m["escalated_kinds"] == {} and m["outage_rate"] == 1.0  # per call row


def test_scope_accuracy_is_scored_from_the_verifier_outcome(tmp_path):
    scope = []
    for h, verdict_scope, outcome in (
        ("h1", "small", "outage"),
        ("h2", "small", "interrupted"),
        ("h3", "small", "error"),
        ("h4", "small", "done"),
    ):
        scope.append({"ts": 1, "type": "verdict", "hash": h, "verdict": {"scope": verdict_scope}})
        scope.append({"ts": 2, "type": "outcome", "hash": h, "outcome": outcome})
    m = optimizer.collect([], DecisionLog(tmp_path), scope, since=0)
    assert m["scope_judged"] == 2 and m["scope_accuracy"] == 0.5 and m["turn_completion_rate"] == 0.5


def test_rollback_restores_the_live_settings_not_only_the_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("context:\n  compact_input_chars: 7000\ntask_tiers:\n  title: fast\n")
    live = Settings(context={"compact_input_chars": 7000}, task_tiers={"title": "fast"})
    xp = optimizer.Experiments(tmp_path, FakeClock(), live=live)
    xp.start(
        {"title": "t", "patch": {"context": {"compact_input_chars": 9000}, "task_tiers": {"classification": "main"}}},
        metrics_for(0.1),
        sessions=1,
        config_path=cfg,
    )
    assert live.context == {"compact_input_chars": 9000} and live.task_tiers["classification"] == "main"
    xp.rollback("x1", config_path=cfg)
    assert live.context == {"compact_input_chars": 7000} and live.task_tiers == {"title": "fast"}
    assert confio.read_yaml(cfg)["context"]["compact_input_chars"] == 7000


def test_rollback_of_a_key_that_was_absent_resets_the_live_value_to_its_default(tmp_path):
    cfg = tmp_path / "config.yaml"
    live = Settings()
    xp = optimizer.Experiments(tmp_path, FakeClock(), live=live)
    xp.start(
        {"title": "t", "patch": {"context": {"compact_input_chars": 9000}}},
        metrics_for(0.1),
        sessions=1,
        config_path=cfg,
    )
    assert live.context == {"compact_input_chars": 9000}
    xp.rollback("x1", config_path=cfg)
    assert live.context == Settings().context and "context" not in confio.read_yaml(cfg)


def test_a_second_experiment_is_refused_while_one_is_active(tmp_path):
    """Experiments share one score, so a second one (on any key, or a prompt) waits until the first is judged."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("router:\n  max_inline_wait: 20\n")
    xp = optimizer.Experiments(tmp_path, FakeClock())
    xp.start({"title": "x1", "patch": {"router": {"max_inline_wait": 40}}}, metrics_for(0.1), config_path=cfg)
    for overlay in (
        {"title": "x2", "patch": {"router": {"max_inline_wait": 80}}},
        {"title": "x2", "patch": {"task_tiers": {"title": "main"}}},
        {"title": "x2", "name": "brief", "prompt": "Be brief."},
    ):
        with pytest.raises(optimizer.ExperimentBusy, match="x1"):
            xp.start(overlay, metrics_for(0.1), config_path=cfg)
    assert confio.read_yaml(cfg) == {"router": {"max_inline_wait": 40}}
    assert [x["id"] for x in xp.all()] == ["x1"] and not (tmp_path / "overlays").exists()


def test_a_key_is_free_again_once_its_experiment_is_judged(tmp_path):
    cfg = tmp_path / "config.yaml"
    xp = optimizer.Experiments(tmp_path, FakeClock())
    patch = {"router": {"max_inline_wait": 40}}
    xp.start({"title": "x1", "patch": patch}, metrics_for(0.5), sessions=1, config_path=cfg)
    xp.session_done(lambda since: metrics_for(0.9), config_path=cfg)  # worse: rolled back
    assert xp.get("x1")["status"] == "rolled_back"
    xp.start({"title": "x2", "patch": patch}, metrics_for(0.5), sessions=1, config_path=cfg)
    assert xp.get("x2")["status"] == "active"


@pytest.mark.parametrize(("initial", "before_x1"), [("router:\n  max_inline_wait: 20\n", 20), ("", None)])
def test_rolling_back_an_earlier_experiment_leaves_the_value_a_later_one_owns(tmp_path, initial, before_x1):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(initial)
    xp = optimizer.Experiments(tmp_path, FakeClock())
    patch1, patch2 = {"router": {"max_inline_wait": 40}}, {"router": {"max_inline_wait": 80}}
    xp.start({"title": "x1", "patch": patch1}, metrics_for(0.5), sessions=1, config_path=cfg)
    xp.session_done(lambda since: metrics_for(0.1), config_path=cfg)  # better: kept, so x2 may build on it
    xp.start({"title": "x2", "patch": patch2}, metrics_for(0.1), config_path=cfg)

    assert xp.rollback("x1", config_path=cfg)
    assert confio.read_yaml(cfg)["router"]["max_inline_wait"] == 80  # x2 still owns the key
    assert xp.rollback("x2", config_path=cfg)
    # x2 goes back to what the key held before x1, not to the value x1 had set
    assert (confio.read_yaml(cfg).get("router") or {}).get("max_inline_wait") == before_x1


def test_rolling_back_in_the_other_order_restores_each_step(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("router:\n  max_inline_wait: 20\n")
    xp = optimizer.Experiments(tmp_path, FakeClock())
    xp.start(
        {"title": "x1", "patch": {"router": {"max_inline_wait": 40}}}, metrics_for(0.5), sessions=1, config_path=cfg
    )
    xp.session_done(lambda since: metrics_for(0.1), config_path=cfg)
    xp.start({"title": "x2", "patch": {"router": {"max_inline_wait": 80}}}, metrics_for(0.1), config_path=cfg)
    assert xp.rollback("x2", config_path=cfg) and confio.read_yaml(cfg)["router"]["max_inline_wait"] == 40
    assert xp.rollback("x1", config_path=cfg) and confio.read_yaml(cfg)["router"]["max_inline_wait"] == 20


def test_rollback_keeps_a_value_that_was_changed_by_hand_since(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("router:\n  max_inline_wait: 20\n")
    xp = optimizer.Experiments(tmp_path, FakeClock())
    xp.start({"title": "x1", "patch": {"router": {"max_inline_wait": 40}}}, metrics_for(0.1), config_path=cfg)
    cfg.write_text("router:\n  max_inline_wait: 55\n")
    assert xp.rollback("x1", config_path=cfg)
    assert confio.read_yaml(cfg)["router"]["max_inline_wait"] == 55 and xp.get("x1")["status"] == "rolled_back"


async def test_a_key_removed_from_the_config_file_is_reset_on_reload(tmp_path, monkeypatch):
    from k3code.paths import user_config_path
    from test_permissions_gateway import make_server

    server, _ = make_server(tmp_path, ["x"], monkeypatch)
    user_config_path().write_text("max_turns: 9\n", encoding="utf-8")
    server.apply_file_config(tmp_path)
    assert server.config.max_turns == 9
    user_config_path().write_text("output_style: concise\n", encoding="utf-8")  # max_turns removed by hand
    server.apply_file_config(tmp_path)
    assert server.config.max_turns == Settings().max_turns
    await server.close()


def base_metrics(esc, tpt):
    return {
        "escalation_rate": esc,
        "failure_rate": 0.0,
        "loop_guard_per_session": 0.0,
        "approval_prompts_per_session": 0.0,
        "proposal_accept_rate": None,
        "scope_accuracy": None,
        "tokens_per_turn": tpt,
    }


def test_accept_change_needs_the_quality_gain_to_beat_the_token_cost():
    assert not optimizer.accept_change(0.01, 30)  # +1 point for +30% tokens: rejected
    assert optimizer.accept_change(0.10, 5)  # +10 points for +5% tokens: kept


def test_tier_up_is_not_proposed_when_the_main_tier_costs_more_than_it_gains(tmp_path):
    # escalation rate 3/12 = 0.25 (proposed at all); gain 3/10 = 0.3; main costs 2.5x per call: +125% tokens, need 0.625
    m = optimizer.collect(tiered(10, 2, 500, 1500, 3), DecisionLog(tmp_path), [], since=0)
    assert m["escalation_rate"] == 0.25 and optimizer.suggest(m, Settings()) == []
    # no main-tier calls for the kind: nothing measured to weigh, so no tier-up
    m0 = optimizer.collect(tiered(10, 0, 500, 0, 5), DecisionLog(tmp_path), [], since=0)
    assert optimizer.suggest(m0, Settings()) == []


def scope_rows(wrong, right):
    out = []
    for i in range(wrong + right):
        out.append({"ts": 1, "type": "verdict", "hash": f"h{i}", "verdict": {"scope": "small"}})
        out.append({"ts": 2, "type": "outcome", "hash": f"h{i}", "outcome": "error" if i < wrong else "done"})
    return out


def test_gate_widening_is_accepted_only_when_the_planning_cost_is_small(tmp_path):
    cheap = optimizer.collect(
        tiered(0, 0, 0, 0, 0, plan_tok=50)
        + [{**ev(1, "call", "s1", "main", "other"), "tokens_in": 950, "tokens_out": 0, "turn": "o"}],
        DecisionLog(tmp_path),
        scope_rows(4, 6),
        since=0,
    )  # accuracy 0.6 (gain 0.4), planning 5% of tokens
    assert any("Plan more often" in c["title"] for c in optimizer.suggest(cheap, Settings()))
    dear = optimizer.collect(
        tiered(0, 0, 0, 0, 0, plan_tok=900)
        + [{**ev(1, "call", "s1", "main", "other"), "tokens_in": 100, "tokens_out": 0, "turn": "o"}],
        DecisionLog(tmp_path),
        scope_rows(4, 6),
        since=0,
    )  # gain 0.4 needs planning under 80% of tokens; it is 90%
    assert not any("Plan more often" in c["title"] for c in optimizer.suggest(dear, Settings()))


def token_window(escalations, tokens_per_call, n=12):
    """``n`` fresh sessions of 100 calls (one turn each), ``escalations`` of them escalated, at the given tokens."""
    out = []
    for _ in range(n):
        sid = f"w{next(_sids)}"
        for r in rows(0, 100, escalations, session=sid):
            if r["kind"] == "call":
                r = {**r, "tokens_in": tokens_per_call, "tokens_out": 0, "turn": f"{sid}-{r['ts']}"}
            out.append(r)
    return optimizer.collect(out, NO_LOG, [], since=0)


def test_ab_judgement_scores_tokens_as_well_as_quality(tmp_path):
    cfg = tmp_path / "config.yaml"
    xp = optimizer.Experiments(tmp_path, FakeClock())
    patch = {"router": {"max_inline_wait": 40}}
    xp.start({"title": "a", "patch": patch}, token_window(20, 1000), sessions=1, config_path=cfg)
    xp.session_done(lambda since: token_window(19, 1300), lambda text: None, cfg)  # +1 point, +30% tokens
    assert xp.get("x1")["status"] == "rolled_back"
    xp.start({"title": "b", "patch": patch}, token_window(20, 1000), sessions=1, config_path=cfg)
    xp.session_done(lambda since: token_window(10, 1050), lambda text: None, cfg)  # +10 points, +5% tokens
    assert xp.get("x2")["status"] == "kept"
    # symmetric: the same quality for 30% fewer tokens is a gain (it was scored as no change)
    patch = {"router": {"max_inline_wait": 60}}
    xp.start({"title": "c", "patch": patch}, token_window(20, 1000), sessions=1, config_path=cfg)
    xp.session_done(lambda since: token_window(20, 700), lambda text: None, cfg)
    assert xp.get("x3")["status"] == "kept" and xp.get("x3")["result"]["diff"] == 0.15


def test_same_distribution_arms_are_rarely_rolled_back(tmp_path):
    """No real effect: the old before/after comparison of two aggregate scores rolled back about half of these."""
    cfg = tmp_path / "config.yaml"
    rolled_back = 0
    for seed in range(200):
        xp = optimizer.Experiments(tmp_path / str(seed), FakeClock())  # a fresh store: 200 records would slow it down
        rng = random.Random(seed)
        baseline, after = synthetic_window(rng), synthetic_window(rng)
        overlay = {"title": f"t{seed}", "patch": {"router": {"max_inline_wait": 40 + seed}}}
        x = xp.start(overlay, baseline, sessions=1, config_path=cfg)
        xp.session_done(lambda since, after=after: after, config_path=cfg)
        status = xp.get(x["id"])["status"]
        assert status != "active"
        rolled_back += status == "rolled_back"
    assert rolled_back < 20  # under 10% (about 2.5% expected: the interval's upper tail)


def test_a_clearly_worse_overlay_is_rolled_back(tmp_path):
    cfg = tmp_path / "config.yaml"
    xp = optimizer.Experiments(tmp_path, FakeClock())
    for seed in range(5):
        rng = random.Random(seed)
        baseline, worse = synthetic_window(rng, esc_p=0.2), synthetic_window(rng, esc_p=0.6)
        patch = {"router": {"max_inline_wait": 40}}
        x = xp.start({"title": "t", "patch": patch}, baseline, sessions=1, config_path=cfg)
        notes: list[str] = []
        (done,) = xp.session_done(lambda since, worse=worse: worse, notes.append, cfg)
        assert done["id"] == x["id"] and xp.get(x["id"])["status"] == "rolled_back"
        assert done["result"]["ci"][1] < 0 and "rolled back automatically" in notes[0]
        assert "router" not in confio.read_yaml(cfg)


def test_too_few_sessions_per_arm_is_no_decision_and_ends_inconclusive(tmp_path):
    cfg = tmp_path / "config.yaml"
    xp = optimizer.Experiments(tmp_path, FakeClock())
    patch = {"router": {"max_inline_wait": 40}}
    xp.start({"title": "t", "patch": patch}, metrics_for(0.1, n=5), sessions=2, config_path=cfg)
    notes: list[str] = []
    worse = metrics_for(0.9)  # clearly worse, but the control arm has 5 sessions, under MIN_SESSIONS_PER_ARM
    for _ in range(optimizer.MAX_WINDOW_FACTOR * 2 - 1):
        assert xp.session_done(lambda since: worse, notes.append, cfg) == []
    assert xp.get("x1")["status"] == "active" and not notes
    assert confio.read_yaml(cfg)["router"]["max_inline_wait"] == 40
    xp.session_done(lambda since: worse, notes.append, cfg)  # past the longest window: give up, restore
    assert xp.get("x1")["status"] == "inconclusive" and "inconclusive" in notes[0] and "too few" in notes[0]
    assert "router" not in confio.read_yaml(cfg)


def test_an_overlay_that_cannot_be_shown_no_worse_is_inconclusive():
    noisy = [0.3, -0.3] * 6  # the same mean in both arms, but far too noisy to rule out a loss beyond the margin
    r = optimizer.decide(noisy, noisy, seed=1)
    assert r["decision"] == "inconclusive" and r["ci"][0] < -optimizer.ROLLBACK_MARGIN and r["ci"][1] >= 0
    assert optimizer.decide([0.5] * 9, [0.5] * 20, seed=1)["decision"] is None


def test_the_optimizer_allowlist_rejects_permissions_sandbox_caps_halt_channels_and_its_own_gates():
    import pytest

    from k3code.learning.optimizer import check_optimizer_patch
    from k3code.learning.updateconfig import PatchRejected

    for bad in (
        {"permissions": {"bash": {"rm *": "allow"}}},
        {"permission_mode": "yolo"},
        {"sandbox": {"network": True}},
        {"reliability": {"budget": {"tokens": 0}}},
        {"max_turns": 500},
        {"daemon": {"background_paused": False}},
        {"halt": False},
        {"budget_usd": 1000},
        {"notify": {"ntfy": "x"}},
        {"telegram": {"chat": 1}},
        {"learning": {"optimizer": {"enabled": True}}},
        {"learning": {"enabled": False}},
        {"mcp": {"servers": []}},
        {"providers": []},
        {"output_style": "x"},
    ):
        with pytest.raises(PatchRejected):
            check_optimizer_patch(bad)
    for ok in (
        {"task_tiers": {"classification": "main"}},
        {"learning": {"rank_threshold": 0.3}},
        {"router": {"max_inline_wait": 40}},
        {"autonomy": {"gate_modes": ["auto", "default"]}},
        {"context": {"tool_output_chars": 6000, "memory_chars": 8000}},
    ):
        check_optimizer_patch(ok)


def test_an_auto_applied_change_that_regresses_is_rolled_back_and_the_live_value_restored(tmp_path):
    cfg = tmp_path / "config.yaml"
    live = Settings()
    xp = optimizer.Experiments(tmp_path, FakeClock(), live=live)
    xp.start(
        {"title": "clip", "patch": {"context": {"tool_output_chars": 6000}}, "auto": True},
        metrics_for(0.1),
        sessions=1,
        config_path=cfg,
    )
    assert live.context == {"tool_output_chars": 6000} and xp.get("x1")["auto"] is True
    notes: list[str] = []
    xp.session_done(lambda since: metrics_for(0.9), notes.append, cfg)
    assert xp.get("x1")["status"] == "rolled_back" and "rolled back automatically" in notes[0]
    assert live.context == Settings().context and "context" not in confio.read_yaml(cfg)


async def test_a_gate_passing_token_candidate_is_applied_as_an_experiment_and_logged(tmp_path, monkeypatch):
    from test_learning_replay import turn
    from test_permissions_gateway import make_server

    server, _ = make_server(tmp_path, ["x"], monkeypatch, learning={"optimizer": {"enabled": True, "min_sessions": 5}})
    for i in range(6):  # six sessions this week, so the optimizer runs
        server.usage.record(
            "call",
            session=f"s{i}",
            tokens_in=100,
            tokens_out=10,
            tier="main",
            task_kind="interactive_turn",
            turn=f"u{i}",
        )
    turns = [turn(i) for i in range(300)] + [turn(1000, tool_chars=60_000, tools=5)]  # one large turn in 301
    for rec in turns:
        assert server.learning.replays.append(rec)

    await server.learning.run_optimizer()
    assert server.config.context == {"tool_output_chars": 6000}  # live, not only the YAML
    (exp,) = server.learning.experiments.all()
    assert exp["auto"] is True and exp["status"] == "active"
    (row,) = server.learning.log.query("auto_apply", since=0, actor=None)  # logged as an automatic decision
    assert row["detail"]["experiment"] == exp["id"] and row["choice"] == "applied"
    await server.learning.run_optimizer()  # already in effect: no second experiment
    assert len(server.learning.experiments.all()) == 1
    await server.close()


def seed_a_week_with_a_gate_passing_candidate(server):
    """Six sessions this week, and replay records in which one large turn makes ``tool_output_chars: 6000`` pass."""
    from test_learning_replay import turn

    for i in range(6):
        server.usage.record(
            "call",
            session=f"s{i}",
            tokens_in=100,
            tokens_out=10,
            tier="main",
            task_kind="interactive_turn",
            turn=f"u{i}",
        )
    for rec in [turn(i) for i in range(300)] + [turn(1000, tool_chars=60_000, tools=5)]:
        assert server.learning.replays.append(rec)


async def test_optimizer_run_is_analysis_only_while_the_optimizer_is_off(tmp_path, monkeypatch):
    """`/optimizer status` says "off (opt in: ...)", but `run` auto-applied the gate-approved candidate and rewrote
    config.yaml anyway."""
    from k3code.paths import user_config_path
    from test_permissions_gateway import call, make_server

    server, _ = make_server(tmp_path, ["x"], monkeypatch, learning={"optimizer": {"min_sessions": 5}})
    assert server.learning.cfg["optimizer"]["enabled"] is False
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("max_turns: 77\n", encoding="utf-8")
    seed_a_week_with_a_gate_passing_candidate(server)

    out = (await call(server, "command.dispatch", {"name": "optimizer", "arg": "run"}))["output"]

    assert cfg.read_text(encoding="utf-8") == "max_turns: 77\n"  # the user's config is untouched
    assert server.config.context == Settings().context  # and so is the running one
    assert server.learning.experiments.all() == []
    assert server.learning.log.query("auto_apply", since=0, actor=None) == []
    assert "off" in out and "nothing was applied" in out
    (p,) = [x for x in server.learning.store.all() if x.kind == "optimizer"]  # the candidate waits for a human
    assert p.payload["overlay"]["patch"] == {"context": {"tool_output_chars": 6000}}
    await server.close()


async def test_optimizer_run_reports_what_it_applied_automatically(tmp_path, monkeypatch):
    from test_permissions_gateway import call, make_server

    server, _ = make_server(tmp_path, ["x"], monkeypatch, learning={"optimizer": {"enabled": True, "min_sessions": 5}})
    seed_a_week_with_a_gate_passing_candidate(server)

    out = (await call(server, "command.dispatch", {"name": "optimizer", "arg": "run"}))["output"]

    (exp,) = server.learning.experiments.all()
    assert server.config.context == {"tool_output_chars": 6000}
    assert "applied automatically" in out.lower() and exp["id"] in out and exp["title"] in out
    assert f"/optimizer rollback {exp['id']}" in out
    await server.close()


def test_a_replayed_candidate_that_fails_the_gate_becomes_a_proposal_for_a_human(tmp_path):
    from k3code.autonomy.proposals import ProposalStore

    store = ProposalStore(tmp_path)
    cand = {
        "title": "Clip tool results",
        "patch": {"context": {"tool_output_chars": 6000}},
        "evidence": {"token_reduction_pct": 30.0, "pass_drop_points": 9.0},
        "auto_ok": False,
    }
    (p,) = optimizer.propose_replayed([cand], store)
    assert p.kind == "optimizer" and p.payload["overlay"]["patch"] == {"context": {"tool_output_chars": 6000}}
    assert optimizer.propose_replayed([cand], store) == []  # dedup: a dismissed or pending candidate stays put


def test_a_main_tier_kind_without_escalations_is_proposed_one_tier_down(tmp_path):
    m = optimizer.collect(tiered(0, 12, 0, 300, 0, kind="title"), DecisionLog(tmp_path), [], since=0)
    down = [c for c in optimizer.suggest(m, Settings()) if c["patch"] == {"task_tiers": {"title": "cheap"}}]
    assert len(down) == 1 and down[0]["evidence"]["main_calls"] == 12
    # the user's own work is never moved down, and an escalated kind is not
    mine = optimizer.collect(tiered(0, 12, 0, 300, 0, kind="interactive_turn"), DecisionLog(tmp_path), [], since=0)
    assert not [c for c in optimizer.suggest(mine, Settings()) if "cheap" in str(c["patch"])]
    esc = optimizer.collect(tiered(0, 12, 0, 300, 2, kind="title"), DecisionLog(tmp_path), [], since=0)
    assert not [c for c in optimizer.suggest(esc, Settings()) if "cheap" in str(c["patch"])]


async def test_a_rolled_back_auto_candidate_is_not_applied_again_next_week(tmp_path, monkeypatch):
    from test_learning_replay import turn
    from test_permissions_gateway import make_server

    server, _ = make_server(tmp_path, ["x"], monkeypatch, learning={"optimizer": {"enabled": True, "min_sessions": 5}})
    for i in range(6):
        server.usage.record(
            "call",
            session=f"s{i}",
            tokens_in=100,
            tokens_out=10,
            tier="main",
            task_kind="interactive_turn",
            turn=f"u{i}",
        )
    for rec in [turn(i) for i in range(300)] + [turn(1000, tool_chars=60_000, tools=5)]:
        assert server.learning.replays.append(rec)
    await server.learning.run_optimizer()
    (exp,) = server.learning.experiments.all()
    assert server.learning.experiments.rollback(exp["id"])
    assert server.config.context != {"tool_output_chars": 6000}
    await server.learning.run_optimizer()  # the next weekly run sees the same records
    assert len(server.learning.experiments.all()) == 1
    assert server.config.context != {"tool_output_chars": 6000}
    await server.close()


def test_quality_ignores_provider_failovers():
    base = metrics_for(0.2)
    assert optimizer.quality({**base, "failure_rate": 0.05}) == optimizer.quality({**base, "failure_rate": 0.0})
