"""Self-optimizer: metrics → config/prompt overlay proposals → A/B experiment → keep or auto-rollback."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code import confio
from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning import learning_cfg
from k3code.learning.decisions import DecisionLog
from k3code.learning.updateconfig import PatchRejected, check_patch, merge_patch
from k3code.paths import home, user_config_path

ROLLBACK_MARGIN = 0.02


# ── metrics ────────────────────────────────────────────────────────────────

def collect(usage_rows: list[dict[str, Any]], log: DecisionLog, scope_rows: list[dict[str, Any]], *,
            since: float = 0.0, until: float | None = None) -> dict[str, Any]:
    """Aggregate raw rows in [since, until) into rates. ``usage_rows`` are dicts with the usage ``events`` columns."""
    until = until if until is not None else float("inf")
    ev = [r for r in usage_rows if since <= r["ts"] < until]
    sessions = {r["session"] for r in ev if r["session"]}
    n_sess = max(1, len(sessions))
    calls = [r for r in ev if r["kind"] == "call"]
    by_tier: dict[str, dict[str, int]] = {}
    for r in calls:
        by_tier.setdefault(r["tier"] or "main", {"calls": 0, "escalated": 0})["calls"] += 1
    esc_kinds: dict[str, int] = {}
    for r in ev:
        if r["kind"] == "escalated":
            esc_kinds[r["task_kind"]] = esc_kinds.get(r["task_kind"], 0) + 1
            src = (r["detail"] or "").split("->")[0]
            by_tier.setdefault(src or "cheap", {"calls": 0, "escalated": 0})["escalated"] += 1
    for t in by_tier.values():
        t["rate"] = round(t["escalated"] / t["calls"], 3) if t["calls"] else 0.0
    total_calls = max(1, len(calls))
    # M1: tokens per tier, per task kind and per turn (every call row counts, whatever the provider reported)
    tokens_by_tier: dict[str, int] = {}
    tokens_by_kind: dict[str, int] = {}
    turns: set[str] = set()
    for r in calls:
        n = int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
        tokens_by_tier[r["tier"] or "main"] = tokens_by_tier.get(r["tier"] or "main", 0) + n
        if r["task_kind"]:
            tokens_by_kind[r["task_kind"]] = tokens_by_kind.get(r["task_kind"], 0) + n
        if r.get("turn"):
            turns.add(r["turn"])
    call_tokens = sum(tokens_by_tier.values())
    props = [d for d in log.query("proposal", since=since) if d["ts"] < until]
    acc = sum(1 for d in props if d["choice"] == "accept")
    verdicts: dict[str, dict[str, Any]] = {}
    for r in scope_rows:
        if not (since <= r.get("ts", 0) < until):
            continue
        if r["type"] == "verdict":
            verdicts[r["hash"]] = r
        elif r["type"] == "outcome" and r["hash"] in verdicts:
            verdicts[r["hash"]]["outcome"] = r["outcome"]
    judged = [v for v in verdicts.values() if "outcome" in v]
    wrong = sum(1 for v in judged if v["verdict"].get("scope") in ("trivial", "small") and v["outcome"] != "done")
    return {
        "sessions": len(sessions),
        "calls": len(calls),
        "escalation_rate": round(sum(1 for r in ev if r["kind"] == "escalated") / total_calls, 3),
        "failure_rate": round(sum(1 for r in ev if r["kind"] in ("failover",)) / total_calls, 3),
        "approval_prompts_per_session": round(sum(1 for r in ev if r["kind"] == "approval") / n_sess, 2),
        "loop_guard_per_session": round(sum(1 for r in ev if r["kind"] == "loop_guard") / n_sess, 3),
        "waits_per_session": round(sum(1 for r in ev if r["kind"] in ("retry", "pause")) / n_sess, 2),
        "proposals_decided": len(props),
        "proposal_accept_rate": round(acc / len(props), 3) if props else None,
        "scope_judged": len(judged),
        "scope_accuracy": round(1 - wrong / len(judged), 3) if judged else None,
        "tiers": by_tier,
        "escalated_kinds": esc_kinds,
        "tokens": call_tokens,
        "turns": len(turns),
        "tokens_per_turn": round(call_tokens / len(turns), 1) if turns else None,
        "tokens_by_tier": tokens_by_tier,
        "tokens_by_kind": tokens_by_kind,
    }


def score(m: dict[str, Any]) -> float:
    """One comparable number (higher is better) from the headline rates."""
    s = 1.0 - m["escalation_rate"] - m["failure_rate"] - 0.5 * min(1.0, m["loop_guard_per_session"])
    s -= 0.02 * min(10.0, m["approval_prompts_per_session"])
    if m.get("proposal_accept_rate") is not None:
        s += 0.2 * (m["proposal_accept_rate"] - 0.5)
    if m.get("scope_accuracy") is not None:
        s += 0.2 * (m["scope_accuracy"] - 0.5)
    return round(s, 4)


# ── candidates ─────────────────────────────────────────────────────────────

def suggest(m: dict[str, Any], config: Any) -> list[dict[str, Any]]:
    """Overlay candidates ``{title, patch|prompt, evidence}`` from the metrics."""
    out: list[dict[str, Any]] = []
    cfg = learning_cfg(config)
    if m["escalation_rate"] >= 0.25 and m["escalated_kinds"]:
        kind, n = max(m["escalated_kinds"].items(), key=lambda kv: kv[1])
        if kind:
            out.append({"title": f"Route {kind} to the main tier (cheap tier escalated {n}×)",
                        "patch": {"task_tiers": {kind: "main"}},
                        "evidence": {"escalation_rate": m["escalation_rate"], "kind": kind, "count": n}})
    if m["proposals_decided"] >= 10 and (m["proposal_accept_rate"] or 0) < 0.2:
        cur = float(cfg["rank_threshold"])
        out.append({"title": f"Raise the proposer threshold {cur} → {round(min(0.6, cur + 0.1), 2)} "
                             f"(accept rate {m['proposal_accept_rate']})",
                    "patch": {"learning": {"rank_threshold": round(min(0.6, cur + 0.1), 2)}},
                    "evidence": {"accept_rate": m["proposal_accept_rate"], "decided": m["proposals_decided"]}})
    if m["scope_judged"] >= 10 and (m["scope_accuracy"] or 1) < 0.7:
        out.append({"title": "Plan more often: also run the scope gate in default mode "
                             f"(scope verdict accuracy {m['scope_accuracy']})",
                    "patch": {"autonomy": {"gate_modes": ["auto", "default"]}},
                    "evidence": {"scope_accuracy": m["scope_accuracy"], "judged": m["scope_judged"]}})
    if m["waits_per_session"] >= 3:
        cur_wait = float((getattr(config, "router", None) or {}).get("max_inline_wait", 20))
        out.append({"title": f"Raise router.max_inline_wait {cur_wait:g}s → {cur_wait * 2:g}s "
                             f"({m['waits_per_session']} waits/session)",
                    "patch": {"router": {"max_inline_wait": cur_wait * 2}},
                    "evidence": {"waits_per_session": m["waits_per_session"]}})
    if m["loop_guard_per_session"] >= 0.3:
        out.append({"title": "Add a prompt overlay against repeating identical tool calls "
                             f"(loop guard fired {m['loop_guard_per_session']}/session)",
                    "prompt": "If a tool call fails twice with the same arguments, change the approach or ask the "
                              "user instead of retrying it.",
                    "name": "no-repeat-calls",
                    "evidence": {"loop_guard_per_session": m["loop_guard_per_session"]}})
    return out


# ── experiments ────────────────────────────────────────────────────────────

class Experiments:
    """A/B experiments persisted in ``$K3CODE_HOME/learning/experiments.json``."""

    def __init__(self, home_: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.home = Path(home_ or home())
        self.path = self.home / "learning" / "experiments.json"
        self.overlays = self.home / "overlays"
        self.clock = clock

    def all(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, indent=1), encoding="utf-8")

    def get(self, xid: str) -> dict[str, Any] | None:
        return next((x for x in self.all() if x["id"] == xid), None)

    def active(self) -> list[dict[str, Any]]:
        return [x for x in self.all() if x["status"] == "active"]

    def start(self, overlay: dict[str, Any], baseline: dict[str, Any], *, sessions: int = 20,
              config_path: Path | None = None) -> dict[str, Any]:
        """Apply an accepted overlay. Config overlays patch the user config (old values kept for rollback)."""
        items = self.all()
        xid = f"x{len(items) + 1}"
        exp: dict[str, Any] = {"id": xid, "title": overlay.get("title", ""), "status": "active",
                               "started": self.clock(), "target_sessions": sessions, "sessions_done": 0,
                               "baseline": baseline, "baseline_score": score(baseline), "evidence":
                               overlay.get("evidence", {})}
        if "patch" in overlay:
            path = config_path or user_config_path()
            cur = confio.read_yaml(path)
            check_patch(overlay["patch"], cur)
            exp.update(kind="config", patch=overlay["patch"], prev=_prev_values(cur, overlay["patch"]))
            merged = merge_patch(cur, overlay["patch"])
            confio.validate(merged)
            confio.write_yaml(path, merged)
        else:
            exp.update(kind="prompt", name=overlay["name"])
            self.overlays.mkdir(parents=True, exist_ok=True)
            (self.overlays / f"{xid}-{overlay['name']}.md").write_text(overlay["prompt"].strip() + "\n",
                                                                       encoding="utf-8")
        items.append(exp)
        self._save(items)
        return exp

    def rollback(self, xid: str, *, status: str = "rolled_back", config_path: Path | None = None) -> bool:
        items = self.all()
        exp = next((x for x in items if x["id"] == xid), None)
        if exp is None or exp["status"] not in ("active", "kept"):
            return False
        if exp["kind"] == "config":
            path = config_path or user_config_path()
            cur = confio.read_yaml(path)
            confio.write_yaml(path, _restore(cur, exp["patch"], exp["prev"]))
        else:
            f = self.overlays / f"{xid}-{exp['name']}.md"
            if f.is_file():
                f.unlink()
        exp["status"] = status
        exp["ended"] = self.clock()
        self._save(items)
        return True

    def session_done(self, current_metrics: Callable[[float], dict[str, Any]], notify: Callable[[str], None] | None
                     = None, config_path: Path | None = None) -> list[dict[str, Any]]:
        """Count one finished session for every active experiment; judge the ones that reached their target."""
        items = self.all()
        finished = []
        for x in items:
            if x["status"] != "active":
                continue
            x["sessions_done"] += 1
            if x["sessions_done"] >= x["target_sessions"]:
                finished.append(x)
        self._save(items)
        for x in finished:
            m = current_metrics(x["started"])
            after = score(m)
            x["after"], x["after_score"] = m, after
            if after < x["baseline_score"] - ROLLBACK_MARGIN:
                self._store_result(x, "rolled_back")
                self.rollback(x["id"], config_path=config_path)
                msg = (f"Optimizer experiment {x['id']} ({x['title']}) rolled back automatically: score "
                       f"{x['baseline_score']} → {after}.")
            else:
                self._store_result(x, "kept")
                msg = f"Optimizer experiment {x['id']} ({x['title']}) kept: score {x['baseline_score']} → {after}."
            if notify:
                notify(msg)
        return finished

    def _store_result(self, exp: dict[str, Any], status: str) -> None:
        items = self.all()
        for x in items:
            if x["id"] == exp["id"]:
                x.update(after=exp["after"], after_score=exp["after_score"])
                x["status"] = "active" if status == "rolled_back" else status  # rollback() flips it afterwards
                x["ended"] = self.clock()
        self._save(items)

    def format_status(self) -> str:
        items = self.all()
        if not items:
            return "No optimizer experiments yet. /optimizer run analyses the metrics."
        lines = []
        for x in items:
            extra = f"  score {x['baseline_score']} → {x.get('after_score', '?')}" if x["status"] != "active" else ""
            lines.append(f"{x['id']} [{x['status']}] {x['title']}  ({x['sessions_done']}/{x['target_sessions']} "
                         f"sessions){extra}")
        return "\n".join(lines)


def _prev_values(cur: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    prev: dict[str, Any] = {}
    for k, v in patch.items():
        if isinstance(v, dict):
            prev[k] = _prev_values(cur.get(k) if isinstance(cur.get(k), dict) else {}, v)
        else:
            prev[k] = cur.get(k)
    return prev


def _restore(cur: dict[str, Any], patch: dict[str, Any], prev: dict[str, Any]) -> dict[str, Any]:
    out = dict(cur)
    for k, v in patch.items():
        old = prev.get(k)
        if isinstance(v, dict) and isinstance(old, dict):
            sub = _restore(out[k] if isinstance(out.get(k), dict) else {}, v, old)
            if sub:
                out[k] = sub
            else:
                out.pop(k, None)
        elif old is None:
            out.pop(k, None)
        else:
            out[k] = old
    return out


def propose(m: dict[str, Any], config: Any, store: ProposalStore) -> list[Proposal]:
    out = []
    for c in suggest(m, config):
        try:
            if "patch" in c:
                check_patch(c["patch"], {})
        except PatchRejected:
            continue
        p = store.add("optimizer", c["title"], "apply overlay as an A/B experiment", payload={"overlay": c},
                      key=dedup_key("optimizer", json.dumps(c.get("patch") or c.get("name"), sort_keys=True)))
        if p:
            out.append(p)
    return out


def overlay_prompt() -> str:
    """Active prompt overlays (``$K3CODE_HOME/overlays/*.md``) for the system prompt."""
    d = home() / "overlays"
    parts = [f.read_text(encoding="utf-8").strip() for f in sorted(d.glob("*.md"))] if d.is_dir() else []
    return "## Learned guidance (overlay)\n" + "\n".join(f"- {p}" for p in parts if p) if parts else ""


SELF_IMPROVE_DIR = ".k3code/self-improve"


def write_issue_draft(cwd: Path, title: str, body: str, clock: Callable[[], float] = time.time) -> Path:
    d = Path(cwd) / SELF_IMPROVE_DIR
    d.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")[:40] or "issue"
    path = d / f"{int(clock())}-{slug}.md"
    path.write_text(f"# {title}\n\n{body}\n\n_(draft; opening a PR is not automated yet)_\n", encoding="utf-8")
    return path
