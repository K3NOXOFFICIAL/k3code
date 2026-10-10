"""/ultraplan (independent planners + a judge) and /ultracode (plan, fan-out, adversarial review, fix, test)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.artifacts import write_artifact_file
from k3code.autonomy import DEFAULT_MIN_SCOPE
from k3code.autonomy.fanout import FanoutResult, detect_test_command, extract_subtasks, run_tests
from k3code.autonomy.plan_first import PLAN_SECTIONS, parse_plan
from k3code.autonomy.scope import ScopeVerdict
from k3code.memory import fenced
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind
from k3code.subagents import worktree as wt_mod
from k3code.subagents.budget import AgentBudget, BudgetStop
from k3code.subagents.runner import Handle

logger = logging.getLogger(__name__)


async def gather_or_cancel[T](*aws: Awaitable[T]) -> list[T]:
    """``asyncio.gather`` that cancels the siblings when one fails (e.g. ``BudgetStop``) or the caller is cancelled.

    Plain gather re-raised the first error and left the others running: their sub-agents kept working and spending
    past the budget. A sibling waiting in ``SubagentManager.wait`` takes its child down when cancelled."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


ANGLES = {
    "mvp-first": "Find the smallest slice that delivers the goal end to end; defer everything else.",
    "risk-first": "Start from what could go wrong (unknowns, data loss, regressions) and retire risk early.",
    "architecture-first": "Start from the structure: boundaries, interfaces and data flow, then the steps.",
}

LENSES = {
    "correctness": "correctness: logic errors, wrong behaviour versus the task, broken edge cases, missing tests",
    "security/robustness": "security and robustness: injection, unsafe file/shell handling, secrets, error handling, "
    "resource leaks, concurrency and failure modes",
}

DEFAULT_MAX_TOKENS = 2_000_000
DEFAULT_MAX_AGENTS = 12

JUDGE_SYSTEM = (
    "You are the plan judge. You get several independent plans for one task. Score each from 1 to 10 "
    "(feasibility, completeness, risk handling), then write ONE final plan that grafts the best ideas of the "
    'others onto the strongest. Reply with a first line `SCORES: {"<angle>": <score>, ...}` (JSON) and then the '
    "final plan in markdown with exactly the sections: " + ", ".join(f"## {s}" for s in PLAN_SECTIONS) + ". "
    "In Steps, prefer independent steps that can run in parallel and mark them (parallel)."
)


@dataclass
class UltraPlan:
    task: str
    plan: str
    path: Path | None = None
    scores: dict[str, float] = field(default_factory=dict)
    angles: list[str] = field(default_factory=list)
    artifact_id: str | None = None
    judge_note: str = ""


def with_hook_context(task: str, context: str) -> str:
    """``task`` as the agents' prompts state it: with what the user's hooks added (like a normal turn's prompt).

    Only prompts take this; a title, heading or report line keeps the bare task (a hook's output is multi-line)."""
    if not context:
        return task
    return f"{task}\n\n" + fenced("context from the user's hooks:", context)


def planner_prompt(task: str, angle: str) -> str:
    return (
        f"ANGLE: {angle}\nWrite an implementation plan for this task from one angle only: {ANGLES[angle]}\n\n"
        f"Task:\n{task}\n\nStudy the repository first (read-only). Return the plan as markdown with the sections "
        + ", ".join(PLAN_SECTIONS)
        + ". Mark steps that are independent of each other with (parallel)."
    )


def parse_judge(text: str) -> tuple[dict[str, float], str]:
    """Split the judge reply into ({angle: score}, plan markdown)."""
    scores: dict[str, float] = {}
    m = re.search(r"SCORES:\s*(\{.*?\})", text, re.S)
    if m:
        try:
            scores = {str(k): float(v) for k, v in json.loads(m.group(1)).items()}
        except (ValueError, TypeError):
            scores = {}
    body = text[m.end() :] if m else text
    return scores, body.strip()


def parse_findings(text: str) -> list[dict[str, Any]]:
    """The last JSON array of objects in a reviewer's reply."""
    for m in reversed(list(re.finditer(r"\[\s*(?:\{.*?\}\s*,?\s*)*\]", text or "", re.S))):
        try:
            data = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("issue")]
    return []


def dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for f in findings:
        key = (str(f.get("file", "")), str(f.get("line", "")), str(f.get("issue", "")).lower()[:60])
        if key in seen:
            continue
        seen.add(key)
        out.append({**f, "id": f"f{len(out) + 1}"})
    return out


def parse_votes(text: str) -> dict[str, bool]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    votes = data.get("votes", data) if isinstance(data, dict) else {}
    return {str(k): bool(v) for k, v in votes.items()} if isinstance(votes, dict) else {}


def ultra_cfg(config: Any) -> dict[str, Any]:
    user = dict(getattr(config, "ultracode", None) or {})
    return {"max_tokens": DEFAULT_MAX_TOKENS, "max_agents": DEFAULT_MAX_AGENTS, "min_scope": DEFAULT_MIN_SCOPE, **user}


class Ultra:
    def __init__(self, server: Any) -> None:
        self.server = server

    def progress(
        self, session: Any, command: str, phase: str, detail: str = "", budget: AgentBudget | None = None
    ) -> None:
        payload = {"session_id": session.session_id, "command": command, "phase": phase, "detail": detail}
        if budget is not None:
            payload.update(
                agents=budget.agents, tokens=budget.tokens, max_agents=budget.max_agents, max_tokens=budget.max_tokens
            )
        session.emit("ultra.progress", payload, importance="essential")
        session.emit("status.update", {"kind": "status", "text": f"/{command}: {phase}", "state": "working"})

    # ── /ultraplan ──

    async def ultraplan(
        self,
        session: Any,
        task: str,
        *,
        budget: AgentBudget | None = None,
        command: str = "ultraplan",
        angles: list[str] | None = None,
        context: str = "",
    ) -> UltraPlan:
        angles = list(angles or ANGLES)
        brief = with_hook_context(task, context)
        mgr = self.server.subagents
        self.progress(session, command, "planning", f"{len(angles)} independent planners", budget)

        async def one(i: int, angle: str) -> tuple[str, Handle | None]:
            if budget is not None:
                budget.take_agent()
            h = mgr.spawn(
                session,
                description=f"plan: {angle}",
                prompt=planner_prompt(brief, angle),
                agent_type="planner",
                tier="strong",
                index=i,
                count=len(angles),
            )
            await mgr.wait(h)
            if budget is not None:
                budget.charge(h)
            return angle, h

        try:
            done = await gather_or_cancel(*(one(i, a) for i, a in enumerate(angles)))
        except asyncio.CancelledError:
            mgr.interrupt_session(session.session_id)
            raise
        plans = [(a, h.result) for a, h in done if h and h.status == "completed" and h.result.strip()]
        if not plans:
            raise RuntimeError(
                "no planner produced a plan: " + "; ".join(f"{a}: {h.error or h.status}" for a, h in done if h)
            )
        self.progress(session, command, "judging", f"{len(plans)} plans", budget)
        body = "\n\n".join(f"=== Plan ({a}) ===\n{p}" for a, p in plans)
        result = await self.server.model_caller.complete(
            TaskKind.PLAN,
            [Message(role="system", content=JUDGE_SYSTEM), Message(role="user", content=f"Task:\n{brief}\n\n{body}")],
            session_id=session.session_id,
            max_tokens=4096,
        )
        if budget is not None:
            budget.tokens += result.prompt_tokens + result.completion_tokens
        scores, final = parse_judge(result.text)
        note = ""
        if not parse_plan(final):  # the judge did not produce a structured plan: fall back to the best-scored one
            best = max(plans, key=lambda ap: scores.get(ap[0], 0.0))
            final, note = best[1], f"judge reply had no plan sections; used the {best[0]} plan"
        up = UltraPlan(task=task, plan=final, scores=scores, angles=[a for a, _ in plans], judge_note=note)
        up.path, up.artifact_id = self.save_plan(session, up)
        return up

    def save_plan(self, session: Any, up: UltraPlan) -> tuple[Path, str | None]:
        cwd = Path(session.stored.cwd or ".")
        d = cwd / ".k3code" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        gi = cwd / ".k3code" / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        scores = ", ".join(f"{k}: {v:g}" for k, v in up.scores.items()) or "n/a"
        header = f"# Plan: {up.task}\n\n_ultraplan — angles: {', '.join(up.angles)}; judge scores: {scores}_\n\n"
        path = write_artifact_file(self.server, "plan", d, up.task, header + up.plan + "\n", session=session.session_id)
        row = self.server.artifacts.list(session=session.session_id, kind="plan", limit=1)
        return path, (row[0].id if row else None)

    def show_plan(self, session: Any, up: UltraPlan, *, status: str = "proposed") -> None:
        verdict = plan_verdict(up.plan)
        session.emit(
            "plan.show",
            {
                "session_id": session.session_id,
                "plan_id": f"ultraplan-{int(time.time())}",
                "status": status,
                "plan": up.plan,
                "sections": parse_plan(up.plan),
                "scope": verdict.scope,
                "risk": verdict.risk,
                "fanout_candidate": verdict.fanout_candidate,
                "parallelizable": verdict.parallelizable,
                "subtasks": verdict.suggested_subtasks,
                "path": str(up.path or ""),
                "source": "ultraplan",
                "scores": up.scores,
            },
            importance="essential",
        )

    # ── /ultracode ──

    async def ultracode(self, session: Any, task: str, *, context: str = "") -> str:
        cfg = ultra_cfg(self.server.config)
        brief = with_hook_context(task, context)  # the agents' prompts; the report and its title keep ``task``
        budget = AgentBudget(int(cfg["max_agents"]), int(cfg["max_tokens"]))
        report = _Report(task)
        cwd = Path(session.perms.cwd)
        try:
            up = await self.ultraplan(session, task, budget=budget, command="ultracode", context=context)
            report.plan_path = str(up.path)
            self.show_plan(session, up, status="approved")
            base = await wt_mod.head_sha(cwd) if await wt_mod.repo_root(cwd) else ""

            self.progress(session, "ultracode", "implementing", "", budget)
            subtasks = extract_subtasks(plan_verdict(up.plan), up.plan) or [task]
            fan = await self.server.fanout.run(session, brief, up.plan, subtasks, budget=budget)
            report.fanout = fan
            if fan is not None and fan.stopped:
                raise BudgetStop(fan.stopped)
            if fan is None:  # no git repo: one worker, in place
                prompt = f"Implement this task following the plan.\n\nTask:\n{brief}\n\nPlan:\n{up.plan}"
                h = await self.run_child(session, budget, "worker", prompt, "implement")
                report.notes.append(f"implementation (single worker, no git repo): {h.status}")
            elif not fan.ok:
                report.notes.append("fan-out left unresolved subtasks:\n" + fan.summary())

            await self.review_and_fix(session, budget, report, brief, up.plan, base)
            test_cmd = self.server.fanout.cfg.get("test_command") or detect_test_command(cwd) or ""
            if test_cmd:
                self.progress(session, "ultracode", "final tests", test_cmd, budget)
                ok, tail = await run_tests(test_cmd, cwd, float(self.server.fanout.cfg["test_timeout"]))
                report.tests = f"{'pass' if ok else 'FAIL'} ({test_cmd})" + ("" if ok else f"\n{tail[-1500:]}")
            else:
                report.tests = "no test command detected"
        except BudgetStop as e:
            report.stopped = str(e)
            self.progress(session, "ultracode", "stopped", str(e), budget)
        report.budget = budget.summary()
        text = report.render()
        path = write_artifact_file(
            self.server, "review", cwd / ".k3code" / "reports", f"ultracode {task}", text, session=session.session_id
        )
        return text + f"\n\nReport saved: {path}"

    async def run_child(
        self, session: Any, budget: AgentBudget, agent_type: str, prompt: str, desc: str, **kw: Any
    ) -> Handle:
        budget.take_agent()
        h = await self.server.subagents.run(session, description=desc, prompt=prompt, agent_type=agent_type, **kw)
        budget.charge(h)
        return h

    async def review_and_fix(
        self, session: Any, budget: AgentBudget, report: _Report, task: str, plan: str, base: str
    ) -> None:
        cwd = Path(session.perms.cwd)
        diff = ""
        if base:
            _, diff = await wt_mod.git(cwd, "diff", f"{base}..HEAD")
        if not diff.strip():
            report.notes.append("nothing to review: no committed changes")
            return
        diff = diff[:60000]
        self.progress(session, "ultracode", "adversarial review", ", ".join(LENSES), budget)

        async def lens_review(lens: str, desc: str) -> list[dict[str, Any]]:
            prompt = (
                f"LENS: {lens}\nAdversarially review this change through one lens only: {desc}.\n\nTask:\n{task}\n\n"
                f"Diff:\n```diff\n{diff}\n```\nRead the code if you need more context. Reply with a JSON array of "
                'findings: [{"file": "...", "line": 0, "severity": "high|med|low", "issue": "..."}] '
                "(an empty array if you find nothing). Report only real problems."
            )
            h = await self.run_child(session, budget, "reviewer", prompt, f"review: {lens}")
            return parse_findings(h.result)

        results = await gather_or_cancel(*(lens_review(k, v) for k, v in LENSES.items()))
        candidates = dedupe_findings([f for r in results for f in r])
        report.candidates = candidates
        if not candidates:
            report.notes.append("review panel found nothing")
            return
        self.progress(session, "ultracode", "cross-checking findings", f"{len(candidates)} candidates", budget)
        votes: dict[str, dict[str, bool]] = {}
        listing = json.dumps(candidates, indent=1)
        for lens, desc in LENSES.items():
            budget.check()
            res = await self.server.model_caller.complete(
                TaskKind.REVIEW,
                [
                    Message(
                        role="system",
                        content=(
                            f"You are a skeptical reviewer with the {desc} lens. "
                            f"For each candidate finding decide if it is a "
                            'REAL problem in the diff. Reply with JSON only: {"votes": {"<id>": true|false, ...}}.'
                        ),
                    ),
                    Message(
                        role="user",
                        content=f"VOTE LENS: {lens}\nTask:\n{task}\n\nCandidates:\n{listing}\n\n"
                        f"Diff:\n```diff\n{diff}\n```",
                    ),
                ],
                session_id=session.session_id,
                max_tokens=1024,
            )
            budget.tokens += res.prompt_tokens + res.completion_tokens
            votes[lens] = parse_votes(res.text)
        confirmed = [f for f in candidates if all(v.get(f["id"]) is True for v in votes.values())]
        report.confirmed = confirmed
        if not confirmed:
            report.notes.append("no finding was confirmed by both reviewers")
            return
        self.progress(session, "ultracode", "fixing", f"{len(confirmed)} confirmed findings", budget)
        fix_prompt = (
            f"Fix these confirmed review findings (both reviewers agreed they are real). "
            f"Task context:\n{task}\n\nFindings:\n{json.dumps(confirmed, indent=1)}\n\nKeep changes minimal."
        )
        h = await self.run_child(session, budget, "worker", fix_prompt, "fix findings", isolation="worktree")
        report.fix = f"{h.status}; merge: {h.merge}" + (f" ({h.branch})" if h.merge == "conflict" else "")


@dataclass
class _Report:
    task: str
    plan_path: str = ""
    fanout: FanoutResult | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    confirmed: list[dict[str, Any]] = field(default_factory=list)
    fix: str = ""
    tests: str = ""
    notes: list[str] = field(default_factory=list)
    stopped: str = ""
    budget: str = ""

    def render(self) -> str:
        out = [f"# ultracode report\n\nTask: {self.task}"]
        if self.plan_path:
            out.append(f"Plan: {self.plan_path}")
        if self.fanout is not None:
            out.append("\n## Implementation\n" + self.fanout.summary())
        rejected = len(self.candidates) - len(self.confirmed)
        out.append(
            f"\n## Review panel\n{len(self.candidates)} candidate finding(s); {len(self.confirmed)} confirmed "
            f"by both reviewers, {rejected} rejected."
        )
        for f in self.confirmed:
            out.append(f"- [{f.get('severity', '?')}] {f.get('file', '?')}:{f.get('line', '?')} {f['issue']}")
        if self.fix:
            out.append(f"Fix: {self.fix}")
        out += [f"- {n}" for n in self.notes]
        out.append(f"\n## Final tests\n{self.tests or 'not run'}")
        if self.stopped:
            out.append(f"\n## STOPPED\n{self.stopped}. Work that had not started was skipped.")
        out.append(f"\nBudget used: {self.budget}")
        return "\n".join(out)


def plan_verdict(plan: str) -> ScopeVerdict:
    """A large, parallelizable verdict for an ultraplan'd plan (subtasks come from its Steps)."""
    v = ScopeVerdict(
        scope="large",
        needs_plan=True,
        parallelizable=True,
        source="ultraplan",
        reason="/ultraplan",
        fanout_candidate=True,
    )
    v.suggested_subtasks = extract_subtasks(v, plan)
    return v
