#!/usr/bin/env python3
"""M5 scripted demo (temp home, no network): learn `npm test *`, accept the rule, dismiss a proposal for good."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core" / "src"))

tmp = Path(tempfile.mkdtemp(prefix="k3code-m5-demo-"))
os.environ["K3CODE_HOME"] = str(tmp / "home")
proj = tmp / "proj"
proj.mkdir()

from k3code.autonomy.proposals import ProposalStore  # noqa: E402
from k3code.learning import permrules  # noqa: E402
from k3code.learning.decisions import DecisionLog  # noqa: E402

log = DecisionLog(tmp / "home")
store = ProposalStore(tmp / "home")
for i in range(3):
    log.record(
        "approval",
        session=f"session-{i}",
        cwd=str(proj),
        subject="npm test *",
        choice="once",
        detail={"tool": "bash"},
        project=f"path:{proj}",
    )
print("1. approved `npm test` in 3 separate sessions")

(p,) = permrules.to_proposals(permrules.mine(log, cwd=str(proj)), store)
print(f"2. proposal [{p.kind}]: {p.text}")

print("3. accepted ->", permrules.apply(p.payload))
store.set_status(p.id, "accepted")
cfg = proj / ".k3code" / "config.yaml"
print(f"4. {cfg.relative_to(tmp)}:\n{cfg.read_text()}")

for i in range(2):
    log.record(
        "approval",
        session=f"d{i}",
        cwd=str(proj),
        subject="terraform apply *",
        choice="deny",
        detail={"tool": "bash"},
        project=f"path:{proj}",
    )
(q,) = permrules.to_proposals(permrules.mine(log, cwd=str(proj)), store)
print(f"5. new proposal: {q.text}")
store.set_status(q.id, "dismissed")
print("6. dismissed it")
for run in (1, 2):
    log.record(
        "approval",
        session=f"later{run}",
        cwd=str(proj),
        subject="terraform apply *",
        choice="deny",
        detail={"tool": "bash"},
        project=f"path:{proj}",
    )
    again = permrules.to_proposals(permrules.mine(log, cwd=str(proj)), store)
    print(f"7.{run} proposer rerun -> {len(again)} new proposals (statuses: {[x.status for x in store.all()]})")
    assert again == []
print("OK")
