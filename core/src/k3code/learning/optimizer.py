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
#: Scope outcomes that are verifier results: "done" passed, "error" failed. "outage" (providers unreachable),
#: "interrupted" (the user stopped it) and "needs_input" (a budget or loop guard paused it) say nothing about
#: correctness, so they are not judged.
VERIFIED_OUTCOMES = ("done", "error")
#: Kinds never moved down a tier by the optimizer (the user's own work), and the calls a kind needs first.
TIER_DOWN_EXCLUDED = frozenset({"interactive_turn", "subagent", "plan", "review", "advisor"})
TIER_DOWN_MIN_CALLS = 10
#: Cost term: a change must buy this many quality points (as a fraction: 0.005 = half a point) per percent of extra
#: tokens. +1 point for +30% tokens is rejected (0.01 < 0.15); +10 points for +5% tokens is kept (0.10 >= 0.025).
COST_WEIGHT = 0.5

#: What the optimizer may change: an allowlist of settings it tunes. Every config overlay must stay inside it.
TUNABLE_PATHS = (
    "task_tiers",
    "learning.rank_threshold",
    "router.max_inline_wait",
    "autonomy.gate_modes",
    "context.tool_output_chars",
    "context.memory_chars",
    "context.skill_prompt_limit",
    "context.compact_input_chars",
)
#: Hard exclusions, checked first and never overridden: permissions, the sandbox, halt, spend and turn caps and
#: deadlines, notification channels, and the optimizer's own gates (learning.optimizer.*, learning.enabled).
EXCLUDED_SEGMENTS = frozenset(
    {
        "permissions",
        "permission_mode",
        "headless_permission",
        "hardline",
        "sandbox",
        "daemon",
        "halt",
        "budget",
        "budgets",
        "max_turns",
        "deadline",
        "reliability",
        "notify",
        "notification",
        "notifications",
        "channel",
        "ntfy",
        "telegram",
        "optimizer",
        "enabled",
    }
)


# ── metrics ────────────────────────────────────────────────────────────────


def collect(
    usage_rows: list[dict[str, Any]],
    log: DecisionLog,
    scope_rows: list[dict[str, Any]],
    *,
    since: float = 0.0,
    until: float | None = None,
) -> dict[str, Any]:
    """Aggregate raw rows in [since, until) into rates. ``usage_rows`` are dicts with the usage ``events`` columns."""
    until = until if until is not None else float("inf")
    ev = [r for r in usage_rows if since <= r["ts"] < until]
    sessions = {r["session"] for r in ev if r["session"]}
    n_sess = max(1, len(sessions))
    calls = [r for r in ev if r["kind"] == "call"]
    by_tier: dict[str, dict[str, int]] = {}
    for r in calls:
        by_tier.setdefault(r["tier"] or "main", _tier_row())["calls"] += 1
    esc_kinds: dict[str, int] = {}
    for r in ev:
        # "escalated" is a quality signal (the attempt stalled); a provider outage is its own kind and not counted here
        if r["kind"] == "escalated":
            esc_kinds[r["task_kind"]] = esc_kinds.get(r["task_kind"], 0) + 1
            src = (r["detail"] or "").split("->")[0]
            by_tier.setdefault(src or "cheap", _tier_row())["escalated"] += 1
        elif r["kind"] == "loop_guard":  # on every tier; rows from before the tier was recorded came from cheap starts
            by_tier.setdefault(r.get("tier") or "cheap", _tier_row())["loop_guard"] += 1
    for t in by_tier.values():
        t["rate"] = round(t["escalated"] / t["calls"], 3) if t["calls"] else 0.0
        t["loop_guard_rate"] = round(t["loop_guard"] / t["calls"], 3) if t["calls"] else 0.0
    total_calls = max(1, len(calls))
    # M1: tokens per tier, per task kind and per turn (every call row counts, whatever the provider reported)
    tokens_by_tier: dict[str, int] = {}
    tokens_by_kind: dict[str, int] = {}
    kind_tier: dict[str, dict[str, dict[str, int]]] = {}  # kind -> tier -> {"calls", "tokens"}
    turns: set[str] = set()
    for r in calls:
        n = int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
        tier = r["tier"] or "main"
        tokens_by_tier[tier] = tokens_by_tier.get(tier, 0) + n
        if r["task_kind"]:
            tokens_by_kind[r["task_kind"]] = tokens_by_kind.get(r["task_kind"], 0) + n
            cell = kind_tier.setdefault(r["task_kind"], {}).setdefault(tier, {"calls": 0, "tokens": 0})
            cell["calls"] += 1
            cell["tokens"] += n
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
    judged = [v for v in verdicts.values() if v.get("outcome") in VERIFIED_OUTCOMES]
    passed = sum(1 for v in judged if v["outcome"] == "done")
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
        "verifier_pass_rate": round(passed / len(judged), 3) if judged else None,
        "outage_rate": round(sum(1 for r in ev if r["kind"] == "outage") / total_calls, 3),
        "tiers": by_tier,
        "escalated_kinds": esc_kinds,
        "tokens": call_tokens,
        "turns": len(turns),
        "tokens_per_turn": round(call_tokens / len(turns), 1) if turns else None,
        "tokens_by_tier": tokens_by_tier,
        "tokens_by_kind": tokens_by_kind,
        "kind_tier": kind_tier,
    }


def _tier_row() -> dict[str, int]:
    return {"calls": 0, "escalated": 0, "loop_guard": 0}


def accept_change(quality_gain: float, token_increase_pct: float) -> bool:
    """A tier-up or a gate-widening is kept only when its quality gain beats its cost.

    ``quality_gain`` is a fraction (0.10 = ten points); ``token_increase_pct`` is the extra tokens in percent.
    """
    return quality_gain >= COST_WEIGHT * max(0.0, token_increase_pct) / 100.0


def quality(m: dict[str, Any]) -> float:
    """Quality part of the score (higher is better), from the headline rates."""
    s = 1.0 - m["escalation_rate"] - m["failure_rate"] - 0.5 * min(1.0, m["loop_guard_per_session"])
    s -= 0.02 * min(10.0, m["approval_prompts_per_session"])
    if m.get("proposal_accept_rate") is not None:
        s += 0.2 * (m["proposal_accept_rate"] - 0.5)
    if m.get("scope_accuracy") is not None:
        s += 0.2 * (m["scope_accuracy"] - 0.5)
    return s


def score(m: dict[str, Any], baseline: dict[str, Any] | None = None) -> float:
    """One comparable number (higher is better): the quality, minus a cost term against ``baseline``.

    The cost term is ``COST_WEIGHT`` for every 100% that tokens per turn grew over the baseline; without a baseline
    (or without token data) the score is quality alone.
    """
    s = quality(m)
    if baseline is not None and m.get("tokens_per_turn") and baseline.get("tokens_per_turn"):
        growth = m["tokens_per_turn"] / baseline["tokens_per_turn"] - 1.0
        s -= COST_WEIGHT * max(0.0, growth)
    return round(s, 4)


# ── candidates ─────────────────────────────────────────────────────────────


def _tier_up_gain_and_cost(m: dict[str, Any], kind: str, escalated: int) -> tuple[float | None, float]:
    """Quality gain and token cost of moving ``kind`` from the cheap tier to main, from measured calls.

    Gain: the share of the kind's cheap-tier calls that stalled and would be rescued. Cost: the extra tokens the
    kind's cheap calls would cost at the main tier's measured tokens per call, as a share of all tokens. Without main-
    tier calls for the kind there is nothing measured to weigh, so the change is not proposed (None).
    """
    cells = (m.get("kind_tier") or {}).get(kind, {})
    cheap, main = cells.get("cheap"), cells.get("main")
    if not cheap or not main or not cheap["calls"] or not main["calls"]:
        return None, 0.0
    total = m.get("tokens") or 0
    if not total:
        return None, 0.0
    cheap_per = cheap["tokens"] / cheap["calls"]
    main_per = main["tokens"] / main["calls"]
    extra = cheap["calls"] * max(0.0, main_per - cheap_per)
    return min(1.0, escalated / cheap["calls"]), 100.0 * extra / total


def _planning_cost_pct(m: dict[str, Any]) -> float | None:
    """Share of all tokens spent on planning (the plan task kind); None when no planning was measured."""
    plan = (m.get("tokens_by_kind") or {}).get("plan", 0)
    total = m.get("tokens") or 0
    return 100.0 * plan / total if plan and total else None


def suggest(m: dict[str, Any], config: Any) -> list[dict[str, Any]]:
    """Overlay candidates ``{title, patch|prompt, evidence}`` from the metrics."""
    out: list[dict[str, Any]] = []
    cfg = learning_cfg(config)
    if m["escalation_rate"] >= 0.25 and m["escalated_kinds"]:
        kind, n = max(m["escalated_kinds"].items(), key=lambda kv: kv[1])
        gain, cost_pct = _tier_up_gain_and_cost(m, kind, n)
        if kind and gain is not None and accept_change(gain, cost_pct):
            out.append(
                {
                    "title": f"Route {kind} to the main tier (cheap tier escalated {n}×; quality "
                    f"+{gain:.0%} for +{cost_pct:.0f}% tokens)",
                    "patch": {"task_tiers": {kind: "main"}},
                    "evidence": {
                        "escalation_rate": m["escalation_rate"],
                        "kind": kind,
                        "count": n,
                        "quality_gain": round(gain, 3),
                        "token_increase_pct": round(cost_pct, 1),
                    },
                }
            )
    if m["proposals_decided"] >= 10 and (m["proposal_accept_rate"] or 0) < 0.2:
        cur = float(cfg["rank_threshold"])
        out.append(
            {
                "title": f"Raise the proposer threshold {cur} → {round(min(0.6, cur + 0.1), 2)} "
                f"(accept rate {m['proposal_accept_rate']})",
                "patch": {"learning": {"rank_threshold": round(min(0.6, cur + 0.1), 2)}},
                "evidence": {"accept_rate": m["proposal_accept_rate"], "decided": m["proposals_decided"]},
            }
        )
    if m["scope_judged"] >= 10 and (m["scope_accuracy"] or 1) < 0.7:
        gain = 1.0 - float(m["scope_accuracy"])  # share of judged verdicts the gate got wrong
        cost_pct = _planning_cost_pct(m)  # planning tokens today; widening the gate roughly adds that much again
        if cost_pct is not None and accept_change(gain, cost_pct):
            out.append(
                {
                    "title": "Plan more often: also run the scope gate in default mode "
                    f"(scope verdict accuracy {m['scope_accuracy']}; quality +{gain:.0%} for "
                    f"+{cost_pct:.0f}% tokens)",
                    "patch": {"autonomy": {"gate_modes": ["auto", "default"]}},
                    "evidence": {
                        "scope_accuracy": m["scope_accuracy"],
                        "judged": m["scope_judged"],
                        "quality_gain": round(gain, 3),
                        "token_increase_pct": round(cost_pct, 1),
                    },
                }
            )
    # a task kind that ran on the main tier without a single escalation can run one tier down (a human accepts it: a
    # lower tier saves cost, not tokens, so the replay gate never applies it automatically)
    for kind, cells in sorted((m.get("kind_tier") or {}).items()):
        main = cells.get("main")
        if kind in TIER_DOWN_EXCLUDED or not main or m["escalated_kinds"].get(kind):
            continue
        if main["calls"] < TIER_DOWN_MIN_CALLS:
            continue
        out.append(
            {
                "title": f"Run {kind} on the cheap tier ({main['calls']} main-tier calls, no escalation)",
                "patch": {"task_tiers": {kind: "cheap"}},
                "evidence": {"kind": kind, "main_calls": main["calls"], "escalations": 0},
            }
        )
    if m["waits_per_session"] >= 3:
        cur_wait = float((getattr(config, "router", None) or {}).get("max_inline_wait", 20))
        out.append(
            {
                "title": f"Raise router.max_inline_wait {cur_wait:g}s → {cur_wait * 2:g}s "
                f"({m['waits_per_session']} waits/session)",
                "patch": {"router": {"max_inline_wait": cur_wait * 2}},
                "evidence": {"waits_per_session": m["waits_per_session"]},
            }
        )
    if m["loop_guard_per_session"] >= 0.3:
        out.append(
            {
                "title": "Add a prompt overlay against repeating identical tool calls "
                f"(loop guard fired {m['loop_guard_per_session']}/session)",
                "prompt": "If a tool call fails twice with the same arguments, change the approach or ask the "
                "user instead of retrying it.",
                "name": "no-repeat-calls",
                "evidence": {"loop_guard_per_session": m["loop_guard_per_session"]},
            }
        )
    return out


# ── experiments ────────────────────────────────────────────────────────────


class Experiments:
    """A/B experiments persisted in ``$K3CODE_HOME/learning/experiments.json``.

    ``live`` is the running :class:`~k3code.config.Settings`: a config overlay changes it when it starts and the
    rollback restores it, so the running process and the YAML never disagree (before, only the YAML was restored).
    """

    def __init__(self, home_: Path | None = None, clock: Callable[[], float] = time.time, live: Any = None) -> None:
        self.home = Path(home_ or home())
        self.path = self.home / "learning" / "experiments.json"
        self.overlays = self.home / "overlays"
        self.clock = clock
        self.live = live
        #: called with the experiment after a config rollback rewrote the file (the hub syncs the live config)
        self.on_config_rollback: Callable[[dict[str, Any]], None] | None = None

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

    def start(
        self, overlay: dict[str, Any], baseline: dict[str, Any], *, sessions: int = 20, config_path: Path | None = None
    ) -> dict[str, Any]:
        """Apply an accepted overlay. Config overlays patch the user config (old values kept for rollback).

        ``overlay["auto"]`` marks a change the optimizer made by itself (replay gate); it is logged and rolled back
        like any experiment when the A/B window says it made things worse.
        """
        items = self.all()
        xid = f"x{len(items) + 1}"
        exp: dict[str, Any] = {
            "id": xid,
            "title": overlay.get("title", ""),
            "status": "active",
            "started": self.clock(),
            "target_sessions": sessions,
            "sessions_done": 0,
            "baseline": baseline,
            "baseline_score": score(baseline),
            "evidence": overlay.get("evidence", {}),
            "auto": bool(overlay.get("auto")),
        }
        if "patch" in overlay:
            path = config_path or user_config_path()
            cur = confio.read_yaml(path)
            check_patch(overlay["patch"], cur)
            check_optimizer_patch(overlay["patch"])
            exp.update(kind="config", patch=overlay["patch"], prev=_prev_values(cur, overlay["patch"]))
            merged = merge_patch(cur, overlay["patch"])
            confio.validate(merged)
            confio.write_yaml(path, merged)
            self._sync_live(merged, overlay["patch"])
        else:
            exp.update(kind="prompt", name=overlay["name"])
            self.overlays.mkdir(parents=True, exist_ok=True)
            (self.overlays / f"{xid}-{overlay['name']}.md").write_text(
                overlay["prompt"].strip() + "\n", encoding="utf-8"
            )
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
            restored = _restore(confio.read_yaml(path), exp["patch"], exp["prev"])
            confio.write_yaml(path, restored)
            self._sync_live(restored, exp["patch"])
            if self.on_config_rollback is not None:
                self.on_config_rollback(exp)
        else:
            f = self.overlays / f"{xid}-{exp['name']}.md"
            if f.is_file():
                f.unlink()
        exp["status"] = status
        exp["ended"] = self.clock()
        self._save(items)
        return True

    def _sync_live(self, data: dict[str, Any], patch: dict[str, Any]) -> None:
        """Copy the patched top-level keys from a config mapping onto the live settings (defaults where absent)."""
        if self.live is None:
            return
        from k3code.config import Settings  # local: learning is imported by the gateway before config is needed

        validated = Settings.model_validate(data)
        for key in patch:
            new, cur = getattr(validated, key), getattr(self.live, key, None)
            if isinstance(cur, dict) and isinstance(new, dict):
                cur.clear()  # in place, like the hub's rollback hook: holders of the dict see the new values
                cur.update(new)
            else:
                setattr(self.live, key, new)

    def session_done(
        self,
        current_metrics: Callable[[float], dict[str, Any]],
        notify: Callable[[str], None] | None = None,
        config_path: Path | None = None,
        ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Count one finished session for every active experiment (or only ``ids``); judge the ones that reached
        their target."""
        items = self.all()
        finished = []
        for x in items:
            if x["status"] != "active" or (ids is not None and x["id"] not in ids):
                continue
            x["sessions_done"] += 1
            if x["sessions_done"] >= x["target_sessions"]:
                finished.append(x)
        self._save(items)
        for x in finished:
            m = current_metrics(x["started"])
            after = score(m, x.get("baseline"))
            x["after"], x["after_score"] = m, after
            if after < x["baseline_score"] - ROLLBACK_MARGIN:
                self._store_result(x, "rolled_back")
                self.rollback(x["id"], config_path=config_path)
                msg = (
                    f"Optimizer experiment {x['id']} ({x['title']}) rolled back automatically: score "
                    f"{x['baseline_score']} → {after}."
                )
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
            lines.append(
                f"{x['id']} [{x['status']}] {x['title']}  ({x['sessions_done']}/{x['target_sessions']} sessions){extra}"
            )
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


def check_optimizer_patch(patch: dict[str, Any]) -> None:
    """Raise :class:`PatchRejected` unless every leaf of ``patch`` is a tunable setting and none is excluded."""
    for path in _leaf_paths(patch):
        segments = path.split(".")
        if EXCLUDED_SEGMENTS.intersection(segments):
            raise PatchRejected(
                f"{path}: the optimizer may not change permissions, the sandbox, halt, caps, "
                "notification channels or its own gates"
            )
        if not any(path == t or path.startswith(t + ".") for t in TUNABLE_PATHS):
            raise PatchRejected(f"{path}: not a setting the optimizer may tune")


def _leaf_paths(patch: Any, prefix: str = "") -> list[str]:
    if isinstance(patch, dict):
        out: list[str] = []
        for k, v in patch.items():
            out += _leaf_paths(v, f"{prefix}.{k}" if prefix else str(k))
        return out or ([prefix] if prefix else [])
    return [prefix]


def propose(m: dict[str, Any], config: Any, store: ProposalStore) -> list[Proposal]:
    out = []
    for c in suggest(m, config):
        try:
            if "patch" in c:
                check_patch(c["patch"], {})
                check_optimizer_patch(c["patch"])
        except PatchRejected:
            continue
        p = store.add(
            "optimizer",
            c["title"],
            "apply overlay as an A/B experiment",
            payload={"overlay": c},
            key=dedup_key("optimizer", json.dumps(c.get("patch") or c.get("name"), sort_keys=True)),
        )
        if p:
            out.append(p)
    return out


def propose_replayed(candidates: list[dict[str, Any]], store: ProposalStore) -> list[Proposal]:
    """Proposals (human accept) for replayed candidates that did not pass the auto-apply gate."""
    out = []
    for c in candidates:
        try:
            check_patch(c["patch"], {})
            check_optimizer_patch(c["patch"])
        except PatchRejected:
            continue
        p = store.add(
            "optimizer",
            c["title"],
            "apply overlay as an A/B experiment",
            payload={"overlay": {"title": c["title"], "patch": c["patch"], "evidence": c["evidence"]}},
            key=dedup_key("optimizer", json.dumps(c["patch"], sort_keys=True)),
        )
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
