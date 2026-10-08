from types import SimpleNamespace

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
    return {"ts": ts, "session": session, "kind": kind, "provider": "", "model": "", "detail": detail, "tier": tier,
            "task_kind": task_kind}


def rows(t0, calls, escalations, session="s1", kind="classification", approvals=0):
    out = [ev(t0 + i, "call", session, "cheap", kind) for i in range(calls)]
    out += [ev(t0 + 50 + i, "escalated", session, "main", kind, "cheap->main: x") for i in range(escalations)]
    out += [ev(t0 + 80 + i, "approval", session) for i in range(approvals)]
    return out


def test_collect_metrics(tmp_path):
    log = DecisionLog(tmp_path)
    for c in ("accept", "dismiss", "dismiss"):
        log.record("proposal", choice=c, detail={"kind": "x"}, ts=105)
    scope = [{"ts": 100, "type": "verdict", "hash": "h1", "verdict": {"scope": "small"}},
             {"ts": 101, "type": "outcome", "hash": "h1", "outcome": "error"},
             {"ts": 102, "type": "verdict", "hash": "h2", "verdict": {"scope": "small"}},
             {"ts": 103, "type": "outcome", "hash": "h2", "outcome": "done"}]
    m = optimizer.collect(rows(100, 10, 4, approvals=6), log, scope, since=100)
    assert m["escalation_rate"] == 0.4 and m["approval_prompts_per_session"] == 6
    assert m["tiers"]["cheap"]["rate"] == 0.4 and m["escalated_kinds"] == {"classification": 4}
    assert m["proposal_accept_rate"] == round(1 / 3, 3) and m["scope_accuracy"] == 0.5


def test_suggest_from_metrics(tmp_path):
    log = DecisionLog(tmp_path)
    m = optimizer.collect(rows(0, 10, 5), log, [], since=0)
    (c,) = optimizer.suggest(m, Settings())
    assert c["patch"] == {"task_tiers": {"classification": "main"}} and c["evidence"]["count"] == 5
    store = ProposalStore(tmp_path)
    (p,) = optimizer.propose(m, Settings(), store)
    assert p.kind == "optimizer" and p.payload["overlay"]["evidence"]
    assert optimizer.propose(m, Settings(), store) == []


def metrics_for(esc_rate):
    return optimizer.collect(rows(0, 10, int(10 * esc_rate)), SimpleNamespace(query=lambda *a, **k: []), [], since=0)


def test_ab_worse_metrics_rolls_back_automatically(tmp_path):
    clock = FakeClock()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("max_turns: 7\ntask_tiers:\n  title: fast\n")
    xp = optimizer.Experiments(tmp_path, clock)
    overlay = {"title": "Route classification to main", "patch": {"task_tiers": {"classification": "main"},
                                                                  "learning": {"rank_threshold": 0.3}}}
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
    xp.start({"title": "t", "patch": {"router": {"max_inline_wait": 40}}}, metrics_for(0.5), sessions=1,
             config_path=cfg)
    notes: list[str] = []
    xp.session_done(lambda since: metrics_for(0.1), notes.append, cfg)
    assert xp.get("x1")["status"] == "kept" and "kept" in notes[0]
    assert confio.read_yaml(cfg)["router"]["max_inline_wait"] == 40
    assert xp.rollback("x1", config_path=cfg) and "router" not in confio.read_yaml(cfg)  # manual rollback of a kept one


def test_prompt_overlay_ab_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    xp = optimizer.Experiments(tmp_path, FakeClock())
    xp.start({"title": "p", "name": "no-repeat", "prompt": "Do not repeat failing calls."}, metrics_for(0.1),
             sessions=1)
    assert "Do not repeat failing calls." in optimizer.overlay_prompt()
    from k3code.prompting import build_system_prompt

    assert "Do not repeat failing calls." in build_system_prompt("base", cwd=tmp_path, config=Settings())
    xp.session_done(lambda since: metrics_for(0.7))
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
        xp.start({"title": "x", "patch": {"mem0": {"api_key_env": "A"}}}, metrics_for(0.1),
                 config_path=tmp_path / "c.yaml")


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
    for h, verdict_scope, outcome in (("h1", "small", "outage"), ("h2", "small", "interrupted"),
                                      ("h3", "small", "error"), ("h4", "small", "done")):
        scope.append({"ts": 1, "type": "verdict", "hash": h, "verdict": {"scope": verdict_scope}})
        scope.append({"ts": 2, "type": "outcome", "hash": h, "outcome": outcome})
    m = optimizer.collect([], DecisionLog(tmp_path), scope, since=0)
    assert m["scope_judged"] == 2 and m["scope_accuracy"] == 0.5 and m["verifier_pass_rate"] == 0.5
