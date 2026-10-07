"""M4a units: tier policy, model specs, escalation, scope parsing/floor, proposal store."""

from __future__ import annotations

import json

import pytest

from k3code.autonomy import autonomy_cfg
from k3code.autonomy.proposals import ProposalStore, dedup_key, parse_proposals
from k3code.autonomy.scope import (
    ScopeLog,
    ScopeVerdict,
    apply_floor,
    from_override,
    parse_verdict,
    prompt_hash,
    repo_summary,
)
from k3code.config import ProviderEntry, Settings
from k3code.routing.tiers import DEFAULT_POLICY, Escalation, TaskKind, Tier, next_tier, tier_for, tier_model_specs

# ── tiers ──


def test_policy_covers_every_kind_with_spec_defaults():
    assert set(DEFAULT_POLICY) == set(TaskKind)
    for kind in ("background_turn", "loop_tick", "cron_job", "title", "compaction", "goal_judge"):
        assert tier_for(kind) is Tier.CHEAP
    for kind in ("plan", "review", "advisor"):
        assert tier_for(kind) is Tier.STRONG
    assert tier_for("preview") is Tier.FAST
    assert tier_for("interactive_turn") is Tier.MAIN
    assert tier_for("classification") is Tier.CHEAP


def test_policy_override_from_config():
    assert tier_for("title", {"title": "fast"}) is Tier.FAST
    assert tier_for("title", {"title": "bogus"}) is Tier.CHEAP  # unknown tier name is ignored


def _config(**kw) -> Settings:
    p = ProviderEntry(
        name="a", kind="openai", base_url="http://a", api_key_env="X",
        models={"default": "m-def", "cheap": "m-cheapkey"},
        tiers={"strong": ["s1", "s2"], "fast": "f1"},
    )
    return Settings(providers=[p], **kw)


def test_tier_model_specs_fall_back_to_models_then_default():
    cfg = _config()
    assert tier_model_specs(cfg, "strong") == [["s1", "s2"]]
    assert tier_model_specs(cfg, "fast") == ["f1"]
    assert tier_model_specs(cfg, "cheap") == ["m-cheapkey"]  # models.cheap (pre-M4a key)
    assert tier_model_specs(cfg, "main") == ["m-def"]  # no tiers.main: models.default


def test_main_tier_prefers_tiers_main_for_default_key_only():
    p = ProviderEntry(name="a", kind="openai", base_url="http://a", api_key_env="X",
                      models={"default": "m-def", "alt": "m-alt"}, tiers={"main": "m-tier-main"})
    cfg = Settings(providers=[p])
    assert tier_model_specs(cfg, "main") == ["m-tier-main"]
    assert tier_model_specs(cfg, "main", key="alt") == ["m-alt"]  # explicit /model key wins


def test_escalation_ladder_and_thresholds():
    assert next_tier("cheap") is Tier.MAIN and next_tier("main") is Tier.STRONG and next_tier("strong") is None
    seen: list[tuple[Tier, Tier, str]] = []
    esc = Escalation(Tier.CHEAP, on_escalate=lambda a, b, r: seen.append((a, b, r)))
    assert esc.record("tool_errors") is None and esc.record("tool_errors") is None
    assert esc.record("tool_errors") is Tier.MAIN  # 3rd repeated tool error
    assert seen == [(Tier.CHEAP, Tier.MAIN, "tool_errors")]
    assert esc.record("loop_guard") is Tier.STRONG  # guard fires once
    assert esc.record("loop_guard") is None  # nowhere to go


def test_escalation_judge_not_done_twice():
    esc = Escalation(Tier.CHEAP)
    assert esc.record("judge_not_done") is None
    assert esc.record("judge_not_done") is Tier.MAIN


def test_autonomy_cfg_defaults_and_overrides():
    cfg = autonomy_cfg(Settings(autonomy={"plan_first": False, "escalate": {"tool_errors": 5}}))
    assert cfg["plan_first"] is False and cfg["gate_modes"] == ["auto"]
    assert cfg["escalate"] == {"tool_errors": 5, "loop_guard": 1, "judge_not_done": 2}


# ── scope ──


def test_parse_verdict_tolerates_fences_and_rejects_garbage():
    text = '```json\n{"scope":"medium","needs_plan":false,"risk":"low","parallelizable":true,' \
           '"suggested_subtasks":["a","b"],"reason":"r"}\n```'
    v = parse_verdict(text)
    assert v is not None and v.scope == "medium" and v.parallelizable and v.suggested_subtasks == ["a", "b"]
    assert parse_verdict("no json here") is None
    assert parse_verdict('{"scope":"gigantic"}') is None


@pytest.mark.parametrize(
    "prompt",
    ["delete the old build dir", "run the db migration", "deploy to prod", "rotate the credentials",
     "git push --force origin main", "rm -rf /tmp/x"],
)
def test_danger_classes_force_plan_and_high_risk(prompt):
    v = apply_floor(ScopeVerdict(scope="trivial", risk="low"), prompt)
    assert v.needs_plan and v.risk == "high" and v.wants_plan


def test_danger_in_predicted_actions_counts():
    v = apply_floor(ScopeVerdict(scope="small", suggested_subtasks=["then drop table users"]), "tidy the schema")
    assert v.needs_plan and v.risk == "high"


def test_wants_plan_by_scope_and_fanout_marking():
    assert not apply_floor(ScopeVerdict(scope="small"), "rename a var").wants_plan
    assert apply_floor(ScopeVerdict(scope="medium"), "add a flag").wants_plan
    big = apply_floor(ScopeVerdict(scope="huge"), "rewrite the app")
    assert big.fanout_candidate and big.wants_plan
    assert not apply_floor(ScopeVerdict(scope="medium"), "add a flag").fanout_candidate


def test_override_keeps_the_danger_floor():
    assert from_override("large", "add tests").wants_plan
    v = from_override("trivial", "delete everything")
    assert v.source == "override" and v.risk == "high" and v.needs_plan


def test_scope_log_roundtrip(tmp_path):
    log = ScopeLog(tmp_path)
    h = log.verdict("do x", ScopeVerdict(scope="small"), "s1")
    log.outcome(h, "done", planned=False)
    rows = log.read()
    assert [r["type"] for r in rows] == ["verdict", "outcome"]
    assert rows[0]["hash"] == h == prompt_hash("do x") and rows[0]["verdict"]["scope"] == "small"
    assert "do x" not in json.dumps(rows)  # only the hash is stored


def test_repo_summary_counts_files(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    (tmp_path / "c.ts").write_text("x")
    s = repo_summary(tmp_path)
    assert s.startswith("3 files") and ".py×2" in s


# ── proposals ──


def test_proposal_dedup_and_dismiss_persist(tmp_path):
    store = ProposalStore(tmp_path)
    a = store.add("also_setup", "Do you want me to also set up CI?", "set up CI")
    assert a is not None and a.id == "p1"
    assert store.add("also_setup", "do you want me to ALSO set up CI?!", "x") is None  # same normalised text
    assert store.set_status("p1", "dismissed").status == "dismissed"
    again = ProposalStore(tmp_path)  # fresh instance reads the file
    assert again.add("also_setup", "Do you want me to also set up CI?", "set up CI") is None  # never comes back
    assert [p.status for p in again.all()] == ["dismissed"]
    assert again.add("improvement", "You could add type hints", "add type hints").id == "p2"
    assert dedup_key("a", "x y") != dedup_key("b", "x y")


def test_parse_proposals_limits_and_validates():
    raw = json.dumps([{"kind": "improvement", "text": f"t{i}", "action": f"a{i}"} for i in range(5)]
                     + [{"kind": "bogus", "text": "x"}])
    assert len(parse_proposals("Sure: " + raw)) == 3
    assert parse_proposals("nothing") == []
    assert parse_proposals("[]") == []
