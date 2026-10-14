"""Self-optimizer: metrics → config/prompt overlay proposals → A/B experiment → keep or auto-rollback.

How an experiment is judged
---------------------------
Only one experiment runs at a time (:meth:`Experiments.start` refuses a second): the score is one global
measurement, so two overlays live at once would each be judged on the other's effect.

Every session is one sample: its score is :func:`session_score` over that session's usage rows (escalation rate,
loop guards, approval prompts, and tokens per turn against the control arm's tokens per turn, rewarded as well as
penalised). The two arms are:

- ``concurrent`` (prompt overlays): each session is assigned to ``treatment`` or ``control`` by a hash of the
  experiment id and the session id (:func:`arm`, 50/50), and control sessions are built without the overlay
  (:func:`overlay_prompt`). Both arms see the same period.
- ``before_after`` (config overlays): a config overlay rewrites the one shared config and the live settings every
  session, router and process reads, so it cannot be held back from some sessions. Control is the sessions of the
  week before the start, treatment the sessions since; a session seen in both is left out of the treatment.

After ``target_sessions`` counted sessions the difference ``mean(treatment) - mean(control)`` gets a 95% percentile
bootstrap interval (:func:`bootstrap_ci`, ``BOOTSTRAP_RESAMPLES`` resamples, seeded from the experiment id, so the
judgement is reproducible). The rule (:func:`decide`), in this order:

1. Fewer than ``MIN_SESSIONS_PER_ARM`` sessions in either arm: no decision; the experiment keeps running. Still too
   few after ``MAX_WINDOW_FACTOR`` times its target: ``inconclusive``.
2. Upper bound below 0 (the overlay is clearly worse): ``rolled_back``.
3. Lower bound above ``-ROLLBACK_MARGIN`` (worse by less than the margin at most): ``kept``.
4. Otherwise ``inconclusive``: the data cannot tell, so the old value is restored (the conservative choice).
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code import confio
from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning import learning_cfg
from k3code.learning.decisions import DecisionLog
from k3code.learning.updateconfig import PatchRejected, check_patch, merge_patch
from k3code.paths import ensure_private_dir, home, private_file, user_config_path

#: Non-inferiority margin: an overlay is kept only when it is, with 95% confidence, at most this much worse.
ROLLBACK_MARGIN = 0.02
#: Sessions each arm needs before an experiment is judged at all.
MIN_SESSIONS_PER_ARM = 10
#: An experiment that still lacks the minimum after this many times its target sessions ends inconclusive.
MAX_WINDOW_FACTOR = 3
BOOTSTRAP_RESAMPLES = 1000
CONFIDENCE = 0.95
#: Turn outcomes the scope gate's verdicts are checked against: "done" (the turn completed) and "error" (it failed).
#: This is turn completion, not a verifier: no test or check ran. "outage" (providers unreachable), "interrupted" (the
#: user stopped it) and "needs_input" (a budget or loop guard paused it) say nothing either way, so they are not
#: judged.
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
        "turn_completion_rate": round(passed / len(judged), 3) if judged else None,
        "outage_rate": round(sum(1 for r in ev if r["kind"] == "outage") / total_calls, 3),
        "tiers": by_tier,
        "escalated_kinds": esc_kinds,
        "tokens": call_tokens,
        "turns": len(turns),
        "tokens_per_turn": round(call_tokens / len(turns), 1) if turns else None,
        "tokens_by_tier": tokens_by_tier,
        "tokens_by_kind": tokens_by_kind,
        "kind_tier": kind_tier,
        "per_session": _per_session(ev),
    }


def _tier_row() -> dict[str, int]:
    return {"calls": 0, "escalated": 0, "loop_guard": 0}


def _per_session(ev: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Counts per session id (the A/B samples): calls, escalations, loop guards, approvals, call tokens, turns."""
    out: dict[str, dict[str, int]] = {}
    turns: dict[str, set[str]] = {}
    for r in ev:
        if not r["session"]:
            continue
        s = out.setdefault(
            r["session"], {"calls": 0, "escalated": 0, "loop_guard": 0, "approvals": 0, "tokens": 0, "turns": 0}
        )
        if r["kind"] == "call":
            s["calls"] += 1
            s["tokens"] += int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
            if r.get("turn"):
                turns.setdefault(r["session"], set()).add(r["turn"])
        elif r["kind"] == "escalated":
            s["escalated"] += 1
        elif r["kind"] == "loop_guard":
            s["loop_guard"] += 1
        elif r["kind"] == "approval":
            s["approvals"] += 1
    for sid, ts in turns.items():
        out[sid]["turns"] = len(ts)
    return out


def accept_change(quality_gain: float, token_increase_pct: float) -> bool:
    """A tier-up or a gate-widening is kept only when its quality gain beats its cost.

    ``quality_gain`` is a fraction (0.10 = ten points); ``token_increase_pct`` is the extra tokens in percent.
    """
    return quality_gain >= COST_WEIGHT * max(0.0, token_increase_pct) / 100.0


def quality(m: dict[str, Any]) -> float:
    """Quality part of the score (higher is better), from the headline rates. ``failure_rate`` (provider failovers)
    is an availability event, not quality, and is left out like the outage rate."""
    s = 1.0 - m["escalation_rate"] - 0.5 * min(1.0, m["loop_guard_per_session"])
    s -= 0.02 * min(10.0, m["approval_prompts_per_session"])
    if m.get("proposal_accept_rate") is not None:
        s += 0.2 * (m["proposal_accept_rate"] - 0.5)
    if m.get("scope_accuracy") is not None:
        s += 0.2 * (m["scope_accuracy"] - 0.5)
    return s


def score(m: dict[str, Any], baseline: dict[str, Any] | None = None) -> float:
    """One comparable number (higher is better): the quality, minus a cost term against ``baseline``.

    The cost term is ``COST_WEIGHT`` for every 100% that tokens per turn grew over the baseline, and the same credit
    for every 100% they shrank; without a baseline (or without token data) the score is quality alone.
    """
    s = quality(m)
    if baseline is not None and m.get("tokens_per_turn") and baseline.get("tokens_per_turn"):
        growth = m["tokens_per_turn"] / baseline["tokens_per_turn"] - 1.0
        s -= COST_WEIGHT * growth
    return round(s, 4)


# ── A/B judgement ──────────────────────────────────────────────────────────


def arm(xid: str, session_id: str) -> str:
    """The arm of ``session_id`` in experiment ``xid``: a stable 50/50 split (not ``hash()``, which is per process)."""
    digest = hashlib.sha256(f"{xid}:{session_id}".encode()).digest()
    return "treatment" if digest[0] & 1 else "control"


def _tokens_per_turn(stats: list[dict[str, int]]) -> float | None:
    turns = sum(s["turns"] for s in stats)
    tokens = sum(s["tokens"] for s in stats if s["turns"])
    return tokens / turns if turns and tokens else None


def session_score(s: dict[str, int], ref_tokens_per_turn: float | None) -> float:
    """One session's score (higher is better): the per-session part of :func:`quality`, minus ``COST_WEIGHT`` per
    100% its tokens per turn lie above ``ref_tokens_per_turn`` (plus the same credit below it, capped at +100%).

    Proposal accept rate and scope accuracy are not per session (their rows carry no session id), so they stay out.
    """
    calls = max(1, s["calls"])
    q = 1.0 - min(1.0, s["escalated"] / calls) - 0.5 * min(1.0, s["loop_guard"])
    q -= 0.02 * min(10.0, s["approvals"])
    if ref_tokens_per_turn and s["turns"] and s["tokens"]:
        q -= COST_WEIGHT * min(1.0, s["tokens"] / s["turns"] / ref_tokens_per_turn - 1.0)
    return q


def arm_scores(treatment: list[dict[str, int]], control: list[dict[str, int]]) -> tuple[list[float], list[float]]:
    """Per-session scores of both arms (sessions without a model call say nothing and are left out), with tokens per
    turn measured against the control arm's for both."""
    treatment = [s for s in treatment if s["calls"]]
    control = [s for s in control if s["calls"]]
    ref = _tokens_per_turn(control)
    return [session_score(s, ref) for s in treatment], [session_score(s, ref) for s in control]


def bootstrap_ci(
    treatment: list[float], control: list[float], *, seed: int, resamples: int = BOOTSTRAP_RESAMPLES
) -> tuple[float, float]:
    """Percentile bootstrap interval (``CONFIDENCE``) of ``mean(treatment) - mean(control)``; deterministic per seed."""
    rng = random.Random(seed)
    nt, nc = len(treatment), len(control)
    diffs = sorted(
        sum(rng.choices(treatment, k=nt)) / nt - sum(rng.choices(control, k=nc)) / nc for _ in range(resamples)
    )
    cut = int((1.0 - CONFIDENCE) / 2 * resamples)
    return diffs[cut], diffs[resamples - 1 - cut]


def decide(treatment: list[float], control: list[float], *, seed: int) -> dict[str, Any]:
    """The decision rule of the module docstring. ``decision`` is None while either arm is below the minimum."""
    out: dict[str, Any] = {"n_treatment": len(treatment), "n_control": len(control), "decision": None}
    if min(len(treatment), len(control)) < MIN_SESSIONS_PER_ARM:
        return out
    lo, hi = bootstrap_ci(treatment, control, seed=seed)
    out["diff"] = round(sum(treatment) / len(treatment) - sum(control) / len(control), 4)
    out["ci"] = [round(lo, 4), round(hi, 4)]
    if hi < 0:
        out["decision"] = "rolled_back"
    elif lo > -ROLLBACK_MARGIN:
        out["decision"] = "kept"
    else:
        out["decision"] = "inconclusive"
    return out


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


class ExperimentBusy(RuntimeError):
    """Another experiment is still active: they share one score, so only one runs at a time."""


def _describe(result: dict[str, Any]) -> str:
    """One line for a judged experiment: the score difference, its interval and the arm sizes."""
    arms = f"{result['n_treatment']} treatment / {result['n_control']} control sessions"
    if "ci" not in result:
        return f"too few sessions to judge ({arms}; {MIN_SESSIONS_PER_ARM} per arm needed)"
    lo, hi = result["ci"]
    return f"score {result['diff']:+.3f} vs control ({CONFIDENCE:.0%} interval {lo:+.3f} to {hi:+.3f}; {arms})"


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
        ensure_private_dir(self.path.parent)
        private_file(self.path).write_text(json.dumps(items, indent=1), encoding="utf-8")

    def get(self, xid: str) -> dict[str, Any] | None:
        return next((x for x in self.all() if x["id"] == xid), None)

    def active(self) -> list[dict[str, Any]]:
        return [x for x in self.all() if x["status"] == "active"]

    def start(
        self, overlay: dict[str, Any], baseline: dict[str, Any], *, sessions: int = 20, config_path: Path | None = None
    ) -> dict[str, Any]:
        """Apply an accepted overlay. Config overlays patch the user config (old values kept for rollback).

        ``overlay["auto"]`` marks a change the optimizer made by itself (replay gate); it is logged and rolled back
        like any experiment when the A/B window says it made things worse. Refused (:class:`ExperimentBusy`) while
        another experiment is active: one at a time.
        """
        items = self.all()
        if busy := [x["id"] for x in items if x["status"] == "active"]:
            raise ExperimentBusy(
                f"experiment {busy[0]} is still running; one experiment at a time (wait for its A/B window or "
                f"/optimizer rollback {busy[0]})"
            )
        per_session = baseline.get("per_session") or {}
        if "patch" in overlay and len(per_session) < MIN_SESSIONS_PER_ARM:
            raise ValueError(
                f"not enough history for a before/after test: {len(per_session)} control sessions, "
                f"{MIN_SESSIONS_PER_ARM} needed"
            )
        xid = f"x{len(items) + 1}"
        exp: dict[str, Any] = {
            "id": xid,
            "title": overlay.get("title", ""),
            "status": "active",
            "started": self.clock(),
            "target_sessions": sessions,
            "sessions_done": 0,
            "baseline": {k: v for k, v in baseline.items() if k != "per_session"},
            "baseline_score": score(baseline),
            "evidence": overlay.get("evidence", {}),
            "auto": bool(overlay.get("auto")),
            "design": "before_after" if "patch" in overlay else "concurrent",
        }
        if "patch" in overlay:  # the week before is the control arm
            exp["control"] = per_session
        if "patch" in overlay:
            path = config_path or user_config_path()
            cur = confio.read_yaml(path)
            check_patch(overlay["patch"], cur)
            check_optimizer_patch(overlay["patch"])
            _check_no_overlap(overlay["patch"], items)
            exp.update(kind="config", patch=overlay["patch"], prev=_prev_values(cur, overlay["patch"]))
            merged = merge_patch(cur, overlay["patch"])
            confio.validate(merged)
            confio.write_yaml(path, merged)
            self._sync_live(merged, overlay["patch"])
        else:
            exp.update(kind="prompt", name=overlay["name"])
            ensure_private_dir(self.overlays)
            private_file(self.overlays / f"{xid}-{overlay['name']}.md").write_text(
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
            cur = confio.read_yaml(path)
            _hand_over(items, exp, cur)
            restored = _restore(cur, exp["patch"], exp["prev"])
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
        their target (see the module docstring). Returns the experiments that were decided."""
        items = self.all()
        due = []
        for x in items:
            if x["status"] != "active" or (ids is not None and x["id"] not in ids):
                continue
            x["sessions_done"] += 1
            if x["sessions_done"] >= x["target_sessions"]:
                due.append(x)
        self._save(items)
        finished = []
        for x in due:
            m = current_metrics(x["started"])
            if "design" not in x:  # started before the A/B design: judge it by the old rule
                self._judge_legacy(x, m, notify, config_path)
                finished.append(x)
                continue
            per_session = m.get("per_session") or {}
            if x.get("design") == "concurrent":
                treatment = [s for sid, s in per_session.items() if arm(x["id"], sid) == "treatment"]
                control = [s for sid, s in per_session.items() if arm(x["id"], sid) == "control"]
            else:
                before = x.get("control") or {}
                treatment = [s for sid, s in per_session.items() if sid not in before]
                control = list(before.values())
            t_scores, c_scores = arm_scores(treatment, control)
            result = decide(t_scores, c_scores, seed=zlib.crc32(x["id"].encode()))
            if result["decision"] is None:
                if x["sessions_done"] < MAX_WINDOW_FACTOR * x["target_sessions"]:
                    continue  # too few sessions in an arm yet: keep collecting
                result["decision"] = "inconclusive"
            x["after"] = {k: v for k, v in m.items() if k != "per_session"}
            x["after_score"], x["result"] = score(m, x.get("baseline")), result
            status = result["decision"]
            self._store_result(x, status)
            if status == "kept":
                msg = f"Optimizer experiment {x['id']} ({x['title']}) kept: {_describe(result)}."
            else:
                self.rollback(x["id"], status=status, config_path=config_path)
                msg = (
                    f"Optimizer experiment {x['id']} ({x['title']}) rolled back automatically: {_describe(result)}."
                    if status == "rolled_back"
                    else f"Optimizer experiment {x['id']} ({x['title']}) inconclusive, the old value is restored: "
                    f"{_describe(result)}."
                )
            finished.append(x)
            if notify:
                notify(msg)
        return finished

    def _judge_legacy(
        self, x: dict[str, Any], m: dict[str, Any], notify: Callable[[str], None] | None, config_path: Path | None
    ) -> None:
        """Experiments without a ``design`` field: compare the window's score to the stored baseline score."""
        after = score(m, x.get("baseline"))
        x["after"], x["after_score"] = {k: v for k, v in m.items() if k != "per_session"}, after
        if after < x["baseline_score"] - ROLLBACK_MARGIN:
            x["result"] = {"decision": "rolled_back"}
            self._store_result(x, "rolled_back")
            self.rollback(x["id"], config_path=config_path)
            msg = (
                f"Optimizer experiment {x['id']} ({x['title']}) rolled back automatically: score "
                f"{x['baseline_score']} → {after}."
            )
        else:
            x["result"] = {"decision": "kept"}
            self._store_result(x, "kept")
            msg = f"Optimizer experiment {x['id']} ({x['title']}) kept: score {x['baseline_score']} → {after}."
        if notify:
            notify(msg)

    def _store_result(self, exp: dict[str, Any], status: str) -> None:
        items = self.all()
        for x in items:
            if x["id"] == exp["id"]:
                x.update(after=exp["after"], after_score=exp["after_score"], result=exp["result"])
                x["status"] = "kept" if status == "kept" else "active"  # rollback() sets the final status afterwards
                x["ended"] = self.clock()
        self._save(items)

    def format_status(self) -> str:
        items = self.all()
        if not items:
            return "No optimizer experiments yet. /optimizer run analyses the metrics."
        lines = []
        for x in items:
            if x["status"] == "active":
                extra = ""
            elif x.get("result"):
                extra = f"  {_describe(x['result'])}"
            else:  # judged before the per-session A/B
                extra = f"  score {x['baseline_score']} → {x.get('after_score', '?')}"
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
    """Undo ``patch`` in ``cur``. A leaf goes back to its ``prev`` value only while it still holds the patch's value:
    any other value belongs to a later experiment or a hand edit, and is left alone."""
    out = dict(cur)
    for k, v in patch.items():
        old = prev.get(k)
        if isinstance(v, dict) and isinstance(old, dict):
            sub = _restore(out[k] if isinstance(out.get(k), dict) else {}, v, old)
            if sub:
                out[k] = sub
            else:
                out.pop(k, None)
        elif out.get(k) != v:
            continue
        elif old is None:
            out.pop(k, None)
        else:
            out[k] = old
    return out


def _paths_overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def _check_no_overlap(patch: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """Raise :class:`PatchRejected` when ``patch`` changes a setting that an active experiment is still judging.

    Two live experiments on one key would each hold the other's value as the one to restore, so the later rollback
    would write a value nobody wants any more.
    """
    for x in items:
        if x.get("kind") != "config" or x["status"] != "active":
            continue
        for mine in _leaf_paths(patch):
            for theirs in _leaf_paths(x["patch"]):
                if _paths_overlap(mine, theirs):
                    raise PatchRejected(
                        f"{mine}: experiment {x['id']} is still judging {theirs}; wait for its A/B window or "
                        f"roll it back first"
                    )


def _leaf_value(data: Any, path: str) -> Any:
    for segment in path.split("."):
        data = data.get(segment) if isinstance(data, dict) else None
    return data


def _hand_over(items: list[dict[str, Any]], exp: dict[str, Any], cur: dict[str, Any]) -> None:
    """Pass what ``exp`` would restore on to the next experiment that changed the same setting again.

    ``_restore`` leaves such a setting alone; when that experiment is rolled back it must restore what the setting
    held before ``exp``, not the value ``exp`` set.
    """
    later = [
        x for x in items[items.index(exp) + 1 :] if x.get("kind") == "config" and x["status"] in ("active", "kept")
    ]
    for path in _leaf_paths(exp["patch"]):
        if _leaf_value(cur, path) == _leaf_value(exp["patch"], path):
            continue
        owner = next((x for x in later if path in _leaf_paths(x["patch"])), None)
        if owner is None:
            continue
        *parents, leaf = path.split(".")
        holder = owner["prev"]
        for segment in parents:
            holder = holder.get(segment) if isinstance(holder, dict) else None
        if isinstance(holder, dict):
            holder[leaf] = _leaf_value(exp["prev"], path)


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


#: The most overlay files, and overlay chars in total, the system prompt carries; the rest is named in a note.
MAX_OVERLAY_FILES = 10
MAX_OVERLAY_CHARS = 4000


def overlay_prompt(session_id: str | None = None) -> str:
    """Active prompt overlays (``$K3CODE_HOME/overlays/*.md``) for the system prompt, capped at
    MAX_OVERLAY_FILES files and MAX_OVERLAY_CHARS chars (every overlay used to be injected, re-sent on every call).

    With ``session_id``, the overlay of an active experiment is left out of a session in its control arm."""
    d = home() / "overlays"
    files = sorted(d.glob("*.md")) if d.is_dir() else []
    if session_id is not None and files:
        control = {
            x["id"]
            for x in Experiments(home()).active()
            if x.get("design") == "concurrent" and arm(x["id"], session_id) == "control"
        }
        files = [f for f in files if f.name.split("-", 1)[0] not in control]
    texts = [f.read_text(encoding="utf-8").strip() for f in files]
    texts = [t for t in texts if t]
    kept: list[str] = []
    used = 0
    for text in texts:
        if len(kept) >= MAX_OVERLAY_FILES or used + len(text) > MAX_OVERLAY_CHARS:
            break
        kept.append(text)
        used += len(text)
    if not texts:
        return ""
    lines = [f"- {t}" for t in kept]
    if skipped := len(texts) - len(kept):
        lines.append(
            f"({skipped} more overlay{'s' if skipped != 1 else ''} not included: the cap is "
            f"{MAX_OVERLAY_FILES} files / {MAX_OVERLAY_CHARS} chars)"
        )
    return "## Learned guidance (overlay)\n" + "\n".join(lines)


SELF_IMPROVE_DIR = ".k3code/self-improve"


def write_issue_draft(cwd: Path, title: str, body: str, clock: Callable[[], float] = time.time) -> Path:
    d = Path(cwd) / SELF_IMPROVE_DIR
    d.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")[:40] or "issue"
    path = d / f"{int(clock())}-{slug}.md"
    path.write_text(f"# {title}\n\n{body}\n\n_(draft; opening a PR is not automated yet)_\n", encoding="utf-8")
    return path
