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
from k3code.autonomy.agents import DEFAULT_MAX_AGENTS, AgentLimits, session_limits
from k3code.autonomy.fanout import IO_HEAVY_RE, FanoutResult, detect_test_command, extract_subtasks, run_tests
from k3code.autonomy.plan_first import PLAN_SECTIONS, parse_plan
from k3code.autonomy.scope import ScopeVerdict
from k3code.autonomy.sizing import (
    REVIEW_HOLD,
    group_steps,
    pick_planners,
    size_agents,
    top_level_steps,
    unit_text,
)
from k3code.memory import fenced
from k3code.paths import ignore_generated
from k3code.providers.types import Message
from k3code.reliability.governor import Budget, Governor
from k3code.routing.tiers import TaskKind
from k3code.subagents import worktree as wt_mod
from k3code.subagents.budget import CAP_SCOPE, AgentBudget, BudgetStop
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

JUDGE_SYSTEM = (
    "You are the plan judge. You get several independent plans for one task. Score each from 1 to 10 "
    "(feasibility, completeness, risk handling), then write ONE final plan that grafts the best ideas of the "
    'others onto the strongest. Reply with a first line `SCORES: {"<angle>": <score>, ...}` (JSON) and then the '
    "final plan in markdown with exactly the sections: " + ", ".join(f"## {s}" for s in PLAN_SECTIONS) + ". "
    "In Steps, prefer independent steps that can run in parallel and mark them (parallel); end each step with the "
    "files it changes as [files: path/a.py, path/b/]."
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
        + ". Mark steps that are independent of each other with (parallel), and end each step with the files it "
        "changes as [files: path/a.py, path/b/]."
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
        #: per running /ultracode, by session: (phase, monotonic time it began); the first entry is the run's start
        self._clocks: dict[str, list[tuple[str, float]]] = {}

    def progress(
        self, session: Any, command: str, phase: str, detail: str = "", budget: AgentBudget | None = None
    ) -> None:
        payload = {"session_id": session.session_id, "command": command, "phase": phase, "detail": detail}
        clock = self._clocks.get(session.session_id) if command == "ultracode" else None
        if clock is not None:  # seconds since the run started: the measure every "it got faster" claim needs
            now = time.monotonic()
            clock.append((phase, now))
            payload["elapsed_s"] = round(now - clock[0][1], 1)
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
        width: int | None = None,
    ) -> UltraPlan:
        angles = list(angles or ANGLES)
        judge_wanted = len(angles) > 1  # more than one planner was asked for: the judge reads what comes back
        brief = with_hook_context(task, context)
        mgr = self.server.subagents
        noun = "planner" if len(angles) == 1 else "independent planners"
        self.progress(session, command, "planning", f"{len(angles)} {noun}", budget)
        gate = asyncio.Semaphore(max(1, width or len(angles)))  # planners running at once: the session's width

        async def one(i: int, angle: str) -> tuple[str, Handle | None]:
            async with gate:
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
            plans = [(a, h.result) for a, h in done if h and h.status == "completed" and h.result.strip()]
            if not plans and len(angles) == 1:  # the only planner failed: one more angle, not the end of the run
                spare = next((a for a in ANGLES if a not in angles), None)
                if spare is not None:
                    angles = [*angles, spare]
                    done = [*done, await one(1, spare)]
        except asyncio.CancelledError:
            mgr.interrupt_session(session.session_id)
            raise
        plans = [(a, h.result) for a, h in done if h and h.status == "completed" and h.result.strip()]
        if not plans:
            raise RuntimeError(
                "no planner produced a plan: " + "; ".join(f"{a}: {h.error or h.status}" for a, h in done if h)
            )
        scores: dict[str, float] = {}
        note = ""
        if not judge_wanted:  # one planner was asked for (a task rated small): there is nothing to judge
            final = plans[0][1]
        else:
            self.progress(session, command, "judging", f"{len(plans)} plans", budget)
            body = "\n\n".join(f"=== Plan ({a}) ===\n{p}" for a, p in plans)
            result = await self.server.model_caller.complete(
                TaskKind.PLAN,
                [
                    Message(role="system", content=JUDGE_SYSTEM),
                    Message(role="user", content=f"Task:\n{brief}\n\n{body}"),
                ],
                session_id=session.session_id,
                max_tokens=4096,
            )
            if budget is not None:
                budget.tokens += result.prompt_tokens + result.completion_tokens
            scores, final = parse_judge(result.text)
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
        ignore_generated(cwd / ".k3code")
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

    async def ultracode(
        self, session: Any, task: str, *, context: str = "", scope: str | None = None, verdict: Any = None
    ) -> str:
        """Plan, fan out, review, fix, test. ``verdict`` is the classifier's rating of the task when there is one (the
        always-on mode path); with dynamic sizing it decides how many planners run. ``scope`` alone (no verdict) only
        bounds the workers."""
        scope = scope or getattr(verdict, "scope", None)
        cfg = ultra_cfg(self.server.config)
        limits = session_limits(self.server.config, session)
        brief = with_hook_context(task, context)  # the agents' prompts; the report and its title keep ``task``
        budget = AgentBudget(limits.budget_agents, int(cfg["max_tokens"]))
        # ultracode.max_tokens is checked between children above, so one worker could spend many times the cap, and
        # parallel workers each overshoot. As a budget of the session's governor it is charged by every child's usage
        # and checked after each model call of every child, so all of them stop right after the call that crosses it.
        rel = session.reliability
        if rel is None:  # a cold session (the job is the first thing it runs) has no reliability bundle yet
            self.server._ensure_router(session.stored.model or None)
            rel = await self.server._reliability_for(session)
        gov = rel.governor if rel is not None else None
        capped = gov is not None and bool(budget.max_tokens)
        if capped:
            gov.add_budget(Budget(scope=CAP_SCOPE, tokens=budget.max_tokens))  # type: ignore[union-attr]
        report = _Report(task)
        cwd = Path(session.perms.cwd)
        self._clocks[session.session_id] = [("start", time.monotonic())]
        try:
            up = await self.ultraplan(
                session,
                task,
                budget=budget,
                command="ultracode",
                context=context,
                angles=pick_planners(verdict) if limits.dynamic else None,
                width=limits.max_parallel,
            )
            report.plan_path = str(up.path)
            self.show_plan(session, up, status="approved")
            base = await wt_mod.head_sha(cwd) if await wt_mod.repo_root(cwd) else ""

            width: int | None = None
            detail = ""
            if limits.dynamic:
                subtasks, width, detail = self.size_fanout(session, limits, up.plan, task, scope, budget)
                report.sizing = detail
            else:
                subtasks = extract_subtasks(plan_verdict(up.plan), up.plan) or [task]
                if limits.auto:  # no sizing, but "auto" still keeps only what the plan uses
                    budget.trim(budget.agents + REVIEW_HOLD + 3 * len(subtasks))
            self.progress(session, "ultracode", "implementing", detail, budget)
            fan = await self.server.fanout.run(session, brief, up.plan, subtasks, budget=budget, width=width)
            report.fanout = fan
            if fan is not None and fan.stopped:
                if fan.stopped_kind == "agents" and budget.reserved:  # the review tail is still paid for: run it
                    report.notes.append(f"fan-out stopped early ({fan.stopped}); what was merged is still reviewed")
                else:
                    raise BudgetStop(fan.stopped, fan.stopped_kind or "agents")
            if fan is None:  # no git repo: one worker, in place
                prompt = f"Implement this task following the plan.\n\nTask:\n{brief}\n\nPlan:\n{up.plan}"
                h = await self.run_child(session, budget, "worker", prompt, "implement")
                report.notes.append(f"implementation (single worker, no git repo): {h.status}")
            elif not fan.ok:
                report.notes.append("fan-out left unresolved subtasks:\n" + fan.summary())

            tree_after_fan = await wt_mod.head_sha(cwd) if fan is not None else ""
            await self.review_and_fix(session, budget, report, brief, up.plan, base)
            await self.final_tests(session, report, budget, cwd, tree_after_fan)
        except BudgetStop as e:
            report.stopped = str(e)
            self.progress(session, "ultracode", "stopped", str(e), budget)
        except BaseException:
            self._clocks.pop(session.session_id, None)  # a failed or cancelled run leaves no clock behind
            if capped:
                gov.remove_budget(CAP_SCOPE)  # type: ignore[union-attr]
            raise
        if capped:
            gov.remove_budget(CAP_SCOPE)  # type: ignore[union-attr]
        report.budget = budget.summary()
        report.timing = timing_line(self._clocks.pop(session.session_id, []), time.monotonic())
        text = report.render()
        path = write_artifact_file(
            self.server, "review", cwd / ".k3code" / "reports", f"ultracode {task}", text, session=session.session_id
        )
        return text + f"\n\nReport saved: {path}"

    def size_fanout(
        self, session: Any, limits: AgentLimits, plan: str, task: str, scope: str | None, budget: AgentBudget
    ) -> tuple[list[str], int, str]:
        """How many workers the plan needs and how many run at once. Reserves the review tail in ``budget`` (and trims
        an ``auto`` budget to what the plan will use). Raises :class:`BudgetStop` when it cannot pay for one worker."""
        steps = top_level_steps(plan)
        units = [unit_text(u) for u in group_steps(steps)]
        io_heavy = any(IO_HEAVY_RE.search(u) for u in units)
        rel = session.reliability
        gov = rel.governor if rel is not None and rel.governor is not None else Governor()
        cap = gov.agent_cap(limits.fanout_width, io_heavy=io_heavy, ceiling=limits.max_parallel)
        tokens_left = None if budget.max_tokens is None else max(0, budget.max_tokens - budget.tokens)
        sized = size_agents(
            units,
            whole=task,
            left=budget.left(reserved=True),
            hold=REVIEW_HOLD,
            width_cap=cap,
            scope=scope,
            tokens_left=tokens_left,
        )
        if sized.workers == 0:
            why = "; ".join(sized.reasons) or "no agents left for the implementation"
            raise BudgetStop(f"agent budget exhausted ({budget.agents}/{budget.max_agents} agents): {why}", "agents")
        budget.reserve(sized.hold)
        if limits.auto:  # what the plan uses: each worker and its reviewer, one rework apiece, and the review tail
            budget.trim(budget.agents + sized.hold + 3 * sized.workers)
        held = f"{sized.hold} held for the review" if sized.hold else "none held for the review"
        detail = sized.describe(len(steps), f"budget {budget.max_agents}: {budget.agents} used, {held}")
        if sized.reasons:
            detail += " (" + "; ".join(sized.reasons) + ")"
        return sized.subtasks, sized.width, detail

    async def final_tests(
        self, session: Any, report: _Report, budget: AgentBudget, cwd: Path, tree_after_fan: str = ""
    ) -> None:
        test_cmd = self.server.fanout.cfg.get("test_command") or detect_test_command(cwd) or ""
        fan = report.fanout
        if fan is not None and not fan.by_state("merged") and not report.fix:
            # nothing was merged and nothing fixed: the tree is as it was, a test run on it says nothing about the work
            report.tests = "not run: nothing was merged"
            return
        if (
            test_cmd
            and tree_after_fan
            and fan is not None
            and fan.tests == "pass"
            and fan.test_command == test_cmd
            and fan.tested_in
            and Path(fan.tested_in).resolve() == Path(cwd).resolve()  # the fan-out tested the repo root
            and not report.fix  # no fixer merged since
            and await wt_mod.head_sha(cwd) == tree_after_fan
            and not (await wt_mod.git(cwd, "status", "--porcelain"))[1].strip()
        ):
            # the last merge ran this very command on this very tree before committing it: a second run is a repeat
            # of a test run that can take minutes
            report.tests = f"pass ({test_cmd}): the last merge ran it on this same tree, not repeated"
            return
        if test_cmd:
            self.progress(session, "ultracode", "final tests", test_cmd, budget)
            ok, tail = await run_tests(test_cmd, cwd, float(self.server.fanout.cfg["test_timeout"]))
            merged = (
                f" on {len(fan.by_state('merged'))}/{len(fan.subtasks)} merged subtasks" if fan and not fan.ok else ""
            )
            report.tests = f"{'pass' if ok else 'FAIL'} ({test_cmd}){merged}" + ("" if ok else f"\n{tail[-1500:]}")
        else:
            report.tests = "no test command detected"

    async def run_child(
        self,
        session: Any,
        budget: AgentBudget,
        agent_type: str,
        prompt: str,
        desc: str,
        *,
        reserved: bool = False,
        **kw: Any,
    ) -> Handle:
        """Run one child to completion. ``reserved`` children (the review lenses, the fixer) may use the agents the
        fan-out left held back."""
        budget.take_agent(reserved=reserved)
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

        missing: list[str] = []  # lenses whose reviewer died

        async def lens_review(lens: str, desc: str) -> list[dict[str, Any]]:
            prompt = (
                f"LENS: {lens}\nAdversarially review this change through one lens only: {desc}.\n\nTask:\n{task}\n\n"
                f"Diff:\n```diff\n{diff}\n```\nRead the code if you need more context. Reply with a JSON array of "
                'findings: [{"file": "...", "line": 0, "severity": "high|med|low", "issue": "..."}] '
                "(an empty array if you find nothing). Report only real problems."
            )
            h = await self.run_child(session, budget, "reviewer", prompt, f"review: {lens}", reserved=True)
            if h.status != "completed":  # no answer is not "no findings"
                budget.stop_if_capped(h)
                missing.append(lens)
                report.notes.append(f"the {lens} reviewer did not finish ({h.error or h.status}): that lens is missing")
                return []
            return parse_findings(h.result)

        results = await gather_or_cancel(*(lens_review(k, v) for k, v in LENSES.items()))
        candidates = dedupe_findings([f for r in results for f in r])
        report.panel_ran = True
        report.candidates = candidates
        if not candidates:
            report.notes.append(
                "review panel found nothing in the lenses that finished" if missing else "review panel found nothing"
            )
            return
        self.progress(session, "ultracode", "cross-checking findings", f"{len(candidates)} candidates", budget)
        listing = json.dumps(candidates, indent=1)
        # a vote judges named findings: the files they name are the part of the diff it needs (the whole 60 000
        # characters went to each of the two votes, for an answer of twenty tokens)
        vote_diff = diff_for_files(diff, {str(f.get("file") or "") for f in candidates})

        async def vote(lens: str, desc: str) -> dict[str, bool]:
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
                        f"Diff:\n```diff\n{vote_diff}\n```",
                    ),
                ],
                session_id=session.session_id,
                max_tokens=1024,
            )
            budget.tokens += res.prompt_tokens + res.completion_tokens
            return parse_votes(res.text)

        budget.check()
        # the two votes are independent reads of the same diff: ask them at the same time
        ballots = await gather_or_cancel(*(vote(k, v) for k, v in LENSES.items()))
        votes = dict(zip(LENSES, ballots, strict=True))
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
        h = await self.run_child(
            session, budget, "worker", fix_prompt, "fix findings", reserved=True, isolation="worktree"
        )
        report.fix = f"{h.status}; merge: {h.merge}" + (f" ({h.branch})" if h.merge == "conflict" else "")


_GIT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unquote_git_path(path: str) -> str:
    """A path as git prints it in a header: non-ASCII bytes come as ``\\303\\251`` inside double quotes."""
    path = path.strip().strip('"')
    if "\\" not in path:
        return path
    raw = _GIT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), path).encode("latin-1", "replace")
    return raw.decode("utf-8", "replace")


def _wanted_path(path: str) -> str:
    """A finding's file as a repository-relative suffix: without ``:line``, ``./``, or the quoting around it."""
    path = re.sub(r":\d+(?:-\d+)?$", "", path.strip().strip('"`'))
    return path.removeprefix("./")


def _names_same_file(wanted: str, header_path: str) -> bool:
    """Whether a finding's ``wanted`` path and a diff header's path are one file: one is the tail of the other at a
    directory boundary (``a.py`` for ``src/a.py``, or an absolute path for a repository-relative one)."""
    a, b = "/" + wanted.lstrip("/"), "/" + header_path.lstrip("/")
    return a.endswith(b) or b.endswith(a)


def diff_for_files(diff: str, files: set[str]) -> str:
    """The parts of a ``git diff`` for the files in ``files`` (paths as a reviewer wrote them: ``a.py`` or ``src/a.py``,
    an absolute path, ``a.py:12`` as grep prints it; git's quoted headers and both names of a rename are read). The
    whole diff when no file is named or when any one of them matches no hunk: a finding that names a file the diff
    does not have must still be judged against something, and a vote judges all its findings on one diff."""
    wanted = {w for f in files if f and f.strip() and (w := _wanted_path(f))}
    if not wanted:
        return diff
    blocks = [b for b in re.split(r"(?m)^(?=diff --git )", diff) if b.startswith("diff --git ")]
    header = re.compile(r'diff --git "?a/(.+?)"? "?b/(.+?)"?$')
    names: list[tuple[str, ...]] = []
    for b in blocks:
        m = header.match(b.split("\n", 1)[0])
        names.append(tuple(_unquote_git_path(g) for g in m.groups()) if m else ())
    kept_idx = {i for i, ns in enumerate(names) if any(_names_same_file(w, n) for w in wanted for n in ns)}
    if any(not any(_names_same_file(w, n) for ns in names for n in ns) for w in wanted):
        return diff
    return "".join(blocks[i] for i in sorted(kept_idx)) or diff


#: which report label each ultra.progress phase counts towards
_TIMING_LABELS = {
    "planning": "planning",
    "judging": "planning",
    "implementing": "implementing",
    "adversarial review": "review",
    "cross-checking findings": "review",
    "fixing": "review",
    "final tests": "tests",
}


def timing_line(marks: list[tuple[str, float]], end: float) -> str:
    """``planning 41s, implementing 180s, review 35s, tests 90s`` from the phases a run went through ("" for none)."""
    spent: dict[str, float] = {}
    for (phase, began), (_, ended) in zip(marks, [*marks[1:], ("end", end)], strict=False):
        label = _TIMING_LABELS.get(phase)
        if label:
            spent[label] = spent.get(label, 0.0) + max(0.0, ended - began)
    return ", ".join(f"{label} {seconds:.0f}s" for label, seconds in spent.items())


@dataclass
class _Report:
    task: str
    plan_path: str = ""
    fanout: FanoutResult | None = None
    #: the lens reviewers ran (a run stopped before them has no findings to report, which is not "0 findings")
    panel_ran: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)
    confirmed: list[dict[str, Any]] = field(default_factory=list)
    fix: str = ""
    tests: str = ""
    notes: list[str] = field(default_factory=list)
    stopped: str = ""
    budget: str = ""
    timing: str = ""
    sizing: str = ""

    def render(self) -> str:
        out = [f"# ultracode report\n\nTask: {self.task}"]
        if self.plan_path:
            out.append(f"Plan: {self.plan_path}")
        if self.fanout is not None:
            out.append("\n## Implementation\n" + self.fanout.summary())
            if unresolved := self.fanout.unresolved_lines():
                out.append("\n## PARTIAL\nThese subtasks were not merged:\n" + "\n".join(unresolved))
        rejected = len(self.candidates) - len(self.confirmed)
        if self.panel_ran:
            out.append(
                f"\n## Review panel\n{len(self.candidates)} candidate finding(s); {len(self.confirmed)} confirmed "
                f"by both reviewers, {rejected} rejected."
            )
        else:
            out.append("\n## Review panel\nnot run: the change was not reviewed by the panel.")
        for f in self.confirmed:
            out.append(f"- [{f.get('severity', '?')}] {f.get('file', '?')}:{f.get('line', '?')} {f['issue']}")
        if self.fix:
            out.append(f"Fix: {self.fix}")
        out += [f"- {n}" for n in self.notes]
        out.append(f"\n## Final tests\n{self.tests or 'not run'}")
        if self.stopped:
            out.append(f"\n## STOPPED\n{self.stopped}. Work that had not started was skipped.")
        out.append(f"\nBudget used: {self.budget}")
        if self.sizing:
            out.append(f"Sizing: {self.sizing}")
        if self.timing:
            out.append(f"Timing: {self.timing}")
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
