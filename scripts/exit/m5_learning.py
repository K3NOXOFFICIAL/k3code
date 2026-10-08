#!/usr/bin/env python3
"""M5 exit checks (learning). All harness behaviour: temp K3CODE_HOME, no network, no live model."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CORE = REPO / "core"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CORE / "src"))
sys.path.insert(0, str(CORE / "tests"))
try:
    import pydantic  # noqa: F401  (core deps present?)
except ImportError:
    os.execvp("uv", ["uv", "run", "--project", str(CORE), "python", __file__, *sys.argv[1:]])

from lib import emit, run  # noqa: E402

M = "M5"
TMP = Path(tempfile.mkdtemp(prefix="exit-m5-"))
os.environ["K3CODE_HOME"] = str(TMP / "home")


def pytest_rows(criterion: str, how: str, nodes: list[str]) -> None:
    rc, out = run(
        ["uv", "run", "--project", str(CORE), "pytest", "-q", "-x", "-p", "no:cacheprovider", *nodes],
        cwd=CORE,
        timeout=300,
    )
    emit(M, criterion, how, "PASS" if rc == 0 else "FAIL", out)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "absent"


def audit_demo() -> None:
    """Scripted demo with an audit of config files, backups and the decision log."""
    from k3code import confio
    from k3code.autonomy.proposals import ProposalStore
    from k3code.learning import permrules
    from k3code.learning.decisions import DecisionLog

    home, proj = TMP / "home", TMP / "proj"
    proj.mkdir(parents=True)
    (home).mkdir(parents=True, exist_ok=True)
    ucfg, pcfg = home / "config.yaml", proj / ".k3code" / "config.yaml"
    ucfg.write_text("permission_mode: default\n")
    log, store = DecisionLog(home), ProposalStore(home)

    def approvals(cmd: str, choice: str, n: int, tag: str) -> None:
        for i in range(n):
            log.record(
                "approval",
                session=f"{tag}{i}",
                cwd=str(proj),
                subject=cmd,
                choice=choice,
                detail={"tool": "bash"},
                project=f"path:{proj}",
            )

    before = (sha(ucfg), sha(pcfg))
    approvals("npm test *", "once", 3, "a")
    (p,) = permrules.to_proposals(permrules.mine(log, cwd=str(proj)), store)
    approvals("terraform apply *", "deny", 2, "d")
    (q,) = permrules.to_proposals(permrules.mine(log, cwd=str(proj)), store)
    unchanged = (sha(ucfg), sha(pcfg)) == before and not confio.backups(ucfg)
    # --- dismiss q for good, accept p
    store.set_status(q.id, "dismissed")
    recur = 0
    for r in range(3):
        approvals("terraform apply *", "deny", 1, f"later{r}")
        recur += len(permrules.to_proposals(permrules.mine(log, cwd=str(proj)), store))
    emit(
        M,
        "Dismissed proposals never recur",
        "scripted demo: dismiss a deny-rule proposal, re-feed 3 more matching "
        "decisions, rerun the proposer each time; plus gateway test (3 sessions after dismiss)",
        "PASS" if recur == 0 else "FAIL",
        f"proposer re-emitted {recur} proposals after dismiss; statuses={[x.status for x in store.all()]}",
    )
    pytest_rows(
        "Dismissed proposals never recur (gateway path)",
        "pytest test_dismissed_rule_proposal_never_returns + test_dismiss_latches",
        [
            "tests/test_learning_gateway.py::test_dismissed_rule_proposal_never_returns",
            "tests/test_learning_permrules.py::test_dismiss_latches",
        ],
    )
    # --- accept p: only now may config change
    permrules.apply(p.payload)
    store.set_status(p.id, "accepted")
    changed = sha(pcfg) != before[1]
    data = confio.read_yaml(pcfg)
    rule_ok = data.get("permissions", {}).get("bash", {}).get("npm test *") == "allow"
    ok = unchanged and changed and rule_ok and sha(ucfg) == before[0]
    emit(
        M,
        "No config change without acceptance (audit of config files, backups, decision log)",
        "scripted demo: hash user+project config before/after proposals were shown (unchanged), then accept -> "
        "only the project config gains exactly the accepted rule",
        "PASS" if ok else "FAIL",
        f"unchanged-before-accept={unchanged} changed-after-accept={changed} rule_written={rule_ok} "
        f"user-config-untouched={sha(ucfg) == before[0]} proposals={[(x.kind, x.status) for x in store.all()]}",
    )
    pytest_rows(
        "No config change without acceptance or allowlisted key (update-config)",
        "pytest test_cancel_leaves_config_untouched, test_secrets_and_hardline_relaxations_rejected, "
        "test_valid_patch_shows_diff_and_applies_after_confirmation (backups + diff + confirm)",
        ["tests/test_learning_updateconfig.py"],
    )


def main() -> None:
    audit_demo()
    pytest_rows(
        "A planted bad overlay is auto-reverted",
        "pytest test_ab_worse_metrics_rolls_back_automatically + "
        "optimizer experiment tests (overlay applied, worse metrics over N sessions -> config restored)",
        ["tests/test_learning_optimizer.py"],
    )
    pytest_rows(
        "A mem0 preference changes behaviour (mocked)",
        "pytest distiller with mocked mem0: preference stored "
        "per fact, no secrets; project-prep/ranking honour learned preferences",
        [
            "tests/test_learning_distiller.py::test_mem0_stored_per_preference_and_no_secrets",
            "tests/test_learning_projectprep.py::test_learned_preference_offers_makefile_and_skips_scratch_dirs",
            "tests/test_learning_ranking.py::test_propose_uses_ranker_threshold_and_preferences",
        ],
    )
    emit(
        M,
        "A mem0 preference changes behaviour (live)",
        "needs a live mem0 server + live model",
        "PENDING",
        "mocked test PASS; live path not exercised here (no mem0 writes from an unattended worker)",
        "In a real k3code session with mem0 configured: save a preference via memory, start a new session, "
        "confirm the behaviour changes; then mark PASS",
    )
    emit(
        M,
        "After 2 weeks of real use: >=3 accepted rules and >=40% fewer approval prompts",
        "needs real use",
        "PENDING",
        "no real-use data yet; decision log in the real $K3CODE_HOME is the evidence source",
        "Use k3code daily for 2 weeks, then run `/self-improve` / read `k3code stats` approval_prompts_per_session "
        "and `/proposals` accepted count; compare to the first week",
    )


main()
