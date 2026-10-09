# Design ported from hermes-agent hermes_cli/approvals_suggest.py (MIT): mine approval history into allowlist
# proposals with an unsafe-binary exclusion list. Re-implemented on the k3code decision log + arity patterns.
"""Permission learning: mine approvals/denials into ``permission_rule`` proposals."""

from __future__ import annotations

import shlex
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning.decisions import DecisionLog, project_id
from k3code.permissions import hardline
from k3code.permissions.rules import Rule
from k3code.permissions.state import load_permissions_config, persist_rules, project_config_path

APPROVED = ("once", "session", "always")

#: Never anchor a proposed allow rule on these, however often they were approved.
UNSAFE_ROOTS = frozenset(
    {
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "dd",
        "fdisk",
        "parted",
        "wipefs",
        "sudo",
        "doas",
        "su",
        "chmod",
        "chown",
        "chgrp",
        "kill",
        "killall",
        "pkill",
        "halt",
        "shutdown",
        "reboot",
        "poweroff",
        "init",
        "del",
        "format",
        "truncate",
        "mkswap",
        "sh",
        "bash",
        "zsh",
        "eval",
        "exec",
        "curl",
        "wget",
        "ssh",
        "scp",
        "xargs",
        "find",
    }
)
UNSAFE_PREFIXES = ("mkfs",)


@dataclass
class Candidate:
    tool: str
    pattern: str
    action: str  # allow | deny
    scope: str  # user | project
    project: str  # project id (project scope) or ""
    cwd: str
    approvals: int
    denials: int
    projects: int
    auto_do: bool = False
    evidence: list[int] = field(default_factory=list)

    @property
    def key(self) -> str:
        return dedup_key("permission_rule", f"{self.action} {self.tool} {self.pattern} {self.scope} {self.project}")

    def text(self) -> str:
        where = "in this project" if self.scope == "project" else "across projects"
        if self.auto_do:
            return (
                f"You approved `{self.pattern}` {self.approvals}× {where} and never denied it. "
                f"Do it automatically in auto mode in future?"
            )
        if self.action == "allow":
            return f"You always allow `{self.pattern}` {where} ({self.approvals}×) → add an allow rule?"
        return f"You keep denying `{self.pattern}` {where} ({self.denials}×) → add a deny rule?"


def unsafe_pattern(pattern: str) -> bool:
    try:
        tokens = shlex.split(pattern)
    except ValueError:
        return True
    if not tokens:
        return True
    root = tokens[0].rsplit("/", 1)[-1]
    if root in UNSAFE_ROOTS or root.startswith(UNSAFE_PREFIXES):
        return True
    if "*" in pattern and hardline.wildcard_unsafe(pattern):
        return True  # `uv *`, `npx *`, `docker run *`: the wildcard covers whatever program they are told to run
    return hardline.check(pattern.replace(" *", "")) is not None or any(c in pattern for c in "$`;|&<>")


def _existing(cwd: str) -> set[tuple[str, str]]:
    from k3code.paths import user_config_path

    have: set[tuple[str, str]] = set()
    for path in (user_config_path(), project_config_path(cwd)):
        rules, _ = load_permissions_config(path)
        have.update((r.tool, r.pattern) for r in rules)
    return have


def mine(
    log: DecisionLog,
    *,
    min_approvals: int = 3,
    min_denials: int = 2,
    user_projects: int = 2,
    cwd: str = "",
    since: float | None = None,
) -> list[Candidate]:
    """Candidates from the approval history. Narrowest pattern = the arity-aware pattern that was logged."""
    stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"ok": defaultdict(int), "no": defaultdict(int), "cwd": {}, "ids": []}
    )
    for row in log.query("approval", since=since):
        tool = row["detail"].get("tool") or "bash"
        if tool != "bash":  # edit/write patterns are exact paths: too narrow to generalise safely
            continue
        for pat in (p.strip() for p in row["subject"].split(", ") if p.strip()):
            e = stats[(tool, pat)]
            proj = row["project"] or row["cwd"]
            e["cwd"].setdefault(proj, row["cwd"])
            (e["ok"] if row["choice"] in APPROVED else e["no"])[proj] += 1
            e["ids"].append(row["id"])
    have = _existing(cwd) if cwd else set()
    out: list[Candidate] = []
    for (tool, pat), e in sorted(stats.items()):
        if (tool, pat) in have:
            continue
        ok, no = sum(e["ok"].values()), sum(e["no"].values())
        if ok >= min_approvals and no == 0 and not unsafe_pattern(pat):
            multi = len(e["ok"]) >= user_projects
            proj = "" if multi else max(e["ok"], key=lambda k: e["ok"][k])
            out.append(
                Candidate(
                    tool,
                    pat,
                    "allow",
                    "user" if multi else "project",
                    proj,
                    e["cwd"].get(proj, "") if proj else "",
                    ok,
                    0,
                    len(e["ok"]),
                    evidence=e["ids"][-10:],
                )
            )
        elif no >= min_denials and ok == 0:
            multi = len(e["no"]) >= user_projects
            proj = "" if multi else max(e["no"], key=lambda k: e["no"][k])
            out.append(
                Candidate(
                    tool,
                    pat,
                    "deny",
                    "user" if multi else "project",
                    proj,
                    e["cwd"].get(proj, "") if proj else "",
                    0,
                    no,
                    len(e["no"]),
                    evidence=e["ids"][-10:],
                )
            )
    out.extend(_auto_do(log, min_approvals, since))
    return out


def _auto_do(log: DecisionLog, n: int, since: float | None) -> list[Candidate]:
    """High-risk plan confirmations always approved → propose auto-approving them in auto mode."""
    rows = [r for r in log.query("plan", since=since) if r["detail"].get("risk") == "high"]
    ok = sum(1 for r in rows if r["choice"] in APPROVED or r["choice"] == "approved")
    bad = len(rows) - ok
    if ok >= n and bad == 0:
        return [
            Candidate(
                "plan",
                "high-risk plans",
                "allow",
                "user",
                "",
                "",
                ok,
                0,
                1,
                auto_do=True,
                evidence=[r["id"] for r in rows[-10:]],
            )
        ]
    return []


def to_proposals(cands: list[Candidate], store: ProposalStore, session: str = "") -> list[Proposal]:
    out = []
    for c in cands:
        p = store.add(
            "permission_rule",
            c.text(),
            f"/proposals accept (adds {c.action} rule {c.pattern})",
            session,
            payload={
                "tool": c.tool,
                "pattern": c.pattern,
                "action": c.action,
                "scope": c.scope,
                "cwd": c.cwd,
                "auto_do": c.auto_do,
                "approvals": c.approvals,
                "denials": c.denials,
                "evidence": c.evidence,
            },
            project=c.project,
            key=c.key,
        )
        if p is not None:
            out.append(p)
    return out


def apply(payload: dict[str, Any], *, cwd: str = "") -> str:
    """Write the accepted rule to project or user config; returns where it went."""
    from k3code.confio import read_yaml, write_yaml
    from k3code.paths import user_config_path

    if payload.get("auto_do"):
        path = user_config_path()
        data = read_yaml(path)
        data.setdefault("autonomy", {})["auto_do_plans"] = True
        write_yaml(path, data)
        return f"auto mode will now approve high-risk plans without asking ({path})"
    rule = Rule(tool=payload["tool"], pattern=payload["pattern"], action=payload["action"])
    if payload.get("scope") == "user":
        path = user_config_path()
    else:
        base = payload.get("cwd") or cwd or "."
        path = project_config_path(Path(base))
    persist_rules(path, [rule])
    return f"{rule.action} rule `{rule.pattern}` written to {path}"


def format_candidates(cands: list[Candidate]) -> str:
    if not cands:
        return "No permission-rule candidates yet (needs repeated, consistent approvals or denials)."
    lines = ["Permission rule candidates:"]
    for c in cands:
        tag = "auto-do" if c.auto_do else c.action
        lines.append(
            f"  [{tag}] {c.tool}: {c.pattern}  — {c.approvals} approved, {c.denials} denied, "
            f"{c.projects} project(s) → {c.scope} config"
        )
    lines.append("Candidates are also offered as proposals; accept with /proposals accept <id>.")
    return "\n".join(lines)


__all__ = ["Candidate", "mine", "to_proposals", "apply", "format_candidates", "project_id"]
