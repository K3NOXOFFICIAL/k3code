from k3code.autonomy.proposals import ProposalStore
from k3code.learning import permrules
from k3code.learning.decisions import DecisionLog
from k3code.permissions.state import load_permissions_config
from learn_helpers import approve


def test_threshold_and_narrowest_pattern(tmp_path):
    log = DecisionLog(tmp_path)
    approve(log, "npm test *", n=2)
    assert permrules.mine(log) == []  # below N=3
    approve(log, "npm test *", n=1, session="t")
    cands = permrules.mine(log)
    assert len(cands) == 1
    c = cands[0]
    assert (c.tool, c.pattern, c.action, c.scope, c.approvals) == ("bash", "npm test *", "allow", "project", 3)
    assert "npm test *" in c.text() and "add an allow rule" in c.text()
    assert permrules.mine(log, min_approvals=4) == []  # configurable


def test_denial_counterexample_blocks_allow_and_repeated_denials_propose_deny(tmp_path):
    log = DecisionLog(tmp_path)
    approve(log, "make deploy *", n=5)
    approve(log, "make deploy *", choice="deny")
    assert permrules.mine(log) == []
    approve(log, "terraform apply *", choice="deny", n=2)
    (c,) = permrules.mine(log)
    assert c.action == "deny" and c.pattern == "terraform apply *" and "deny rule" in c.text()


def test_unsafe_patterns_never_proposed(tmp_path):
    log = DecisionLog(tmp_path)
    for pat in ("rm *", "sudo *", "curl *", "bash *", "git push *;rm x"):
        approve(log, pat, n=6)
    assert permrules.mine(log) == []


def test_user_scope_when_two_projects(tmp_path):
    log = DecisionLog(tmp_path)
    approve(log, "cargo test *", n=2, cwd="/p/one")
    approve(log, "cargo test *", n=1, cwd="/p/two")
    (c,) = permrules.mine(log)
    assert c.scope == "user" and c.projects == 2 and "across projects" in c.text()


def test_accept_writes_rule_to_project_or_user_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir()
    log = DecisionLog(tmp_path)
    approve(log, "npm test *", n=3, cwd=str(proj))
    store = ProposalStore(tmp_path)
    (p,) = permrules.to_proposals(permrules.mine(log), store)
    assert p.kind == "permission_rule"
    msg = permrules.apply(p.payload)
    rules, _ = load_permissions_config(proj / ".k3code" / "config.yaml")
    assert [(r.tool, r.pattern, r.action) for r in rules] == [("bash", "npm test *", "allow")]
    assert "written" in msg
    # already configured -> not proposed again
    assert permrules.mine(log, cwd=str(proj)) == []
    # user scope
    approve(log, "cargo test *", n=2, cwd="/a")
    approve(log, "cargo test *", n=1, cwd="/b")
    (c,) = permrules.mine(log, cwd=str(proj))
    permrules.apply(permrules.to_proposals([c], store)[0].payload)
    rules, _ = load_permissions_config(home / "config.yaml")
    assert rules[0].pattern == "cargo test *"


def test_dismiss_latches(tmp_path):
    log = DecisionLog(tmp_path)
    approve(log, "npm test *", n=3)
    store = ProposalStore(tmp_path)
    (p,) = permrules.to_proposals(permrules.mine(log), store)
    store.set_status(p.id, "dismissed")
    approve(log, "npm test *", n=5, session="later")
    assert permrules.to_proposals(permrules.mine(log), store) == []
    assert [x.status for x in store.all()] == ["dismissed"]


def test_auto_do_for_always_approved_high_risk_plans(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    log = DecisionLog(tmp_path)
    for _ in range(3):
        log.record("plan", subject="confirm", choice="approved", detail={"risk": "high"})
    (c,) = [c for c in permrules.mine(log) if c.auto_do]
    assert "automatically in auto mode" in c.text()
    store = ProposalStore(tmp_path)
    (p,) = permrules.to_proposals([c], store)
    permrules.apply(p.payload)
    from k3code.confio import read_yaml

    assert read_yaml(tmp_path / "home" / "config.yaml")["autonomy"]["auto_do_plans"] is True
    log.record("plan", subject="confirm", choice="rejected", detail={"risk": "high"})
    assert not [c for c in permrules.mine(log) if c.auto_do]
