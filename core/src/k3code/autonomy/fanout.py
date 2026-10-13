# Vendored from hermes-agent@4127d78da84b1eee105f298979cc57cc7457f98d:hermes_cli/kanban_swarm.py (MIT)
# Design port only (decompose -> parallel workers -> review -> sequential merge); no code copied. See VENDOR.toml.
"""Automatic fan-out: run the parallelizable subtasks of a large plan as worktree children.

Per subtask: worker child (own worktree) -> reviewer child against the acceptance criteria -> sequential
merge into the parent checkout with the project's tests run on the merged result before it is committed.
A failing review, merge or test goes back to that child ONCE with the error (when the budget pays for the rework
and its re-review); after that it is escalated to the parent (the branch is kept, the parent model gets the details).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.autonomy import autonomy_cfg
from k3code.autonomy.agents import session_limits
from k3code.autonomy.plan_first import parse_plan
from k3code.autonomy.sizing import REVIEW_ATTEMPTS, WORKER_AGENTS, group_steps, size_agents, top_level_steps, unit_text
from k3code.reliability.governor import Governor
from k3code.subagents import worktree as wt_mod
from k3code.subagents.budget import AgentBudget, BudgetStop
from k3code.subagents.runner import Handle

logger = logging.getLogger(__name__)

MAX_SUBTASKS = 8
IO_HEAVY_RE = re.compile(r"\b(install|build|compile|download|bundle|docker|index|crawl|migrat\w+)\b", re.I)
#: The verdict the reply ends with (markdown emphasis and a full stop may follow): a mention earlier in the reply,
#: a quote of the pass line or a word such as "passing" is no verdict.
VERDICT_RE = re.compile(r"VERDICT:\s*(pass|fail)[\s*_`.]*\Z", re.I)
#: Diff characters a reviewer prompt carries; past it the reviewer is told so and pointed at the worktree.
REVIEW_DIFF_CHARS = 20000
# REVIEW_ATTEMPTS (reviewer runs per review) is defined in sizing, which sizes the agent budget from it.


class ReviewUnavailable(Exception):
    """No reviewer returned a verdict: the gate stays shut and the subtask goes to the parent."""


# ── helpers ──


def extract_subtasks(verdict: Any, plan: str) -> list[str]:
    """Parallelizable subtasks: the classifier's suggestion, else the plan's numbered steps."""
    subs = [s.strip() for s in (getattr(verdict, "suggested_subtasks", None) or []) if str(s).strip()]
    if len(subs) < 2 and getattr(verdict, "parallelizable", False):
        steps = parse_plan(plan).get("Steps", "")
        items = [m.group(2).strip() for m in re.finditer(r"^\s*(\d+[.)]|[-*])\s+(.+)$", steps, re.M)]
        if len(items) >= 2:
            subs = items
    return subs[:MAX_SUBTASKS] if len(subs) >= 2 else []


def detect_test_command(repo: str | Path) -> str | None:
    """pytest, npm test, go test or cargo test, by marker files."""
    root = Path(repo)
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            if "test" in (json.loads(pkg.read_text("utf-8")).get("scripts") or {}):
                return "npm test --silent"
        except (OSError, ValueError):
            pass
    py = [root / n for n in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")]
    if any(p.is_file() for p in py) or (root / "tests").is_dir():
        return "python -m pytest -q -x"
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    return None


async def run_tests(cmd: str, cwd: str | Path, timeout: float = 600) -> tuple[bool, str]:
    """Run the test command in the sandbox; pytest's "no tests collected" (5) counts as a pass.

    Fan-out and ultra run unattended: without a usable bwrap the tests are refused (a failure), never run bare.
    """
    from k3code.reliability import sandbox

    try:
        proc = await sandbox.spawn_unattended_shell(cmd, cwd=cwd)
    except sandbox.SandboxRefused as exc:
        return False, str(exc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"tests timed out after {timeout:.0f}s"
    text = out.decode("utf-8", "replace")
    ok = proc.returncode == 0 or (proc.returncode == 5 and "pytest" in cmd)
    return ok, text[-4000:]


def dirty_text(dirty: list[str]) -> str:
    """Why a checkout with uncommitted tracked changes cannot be fanned out into (and what to do about it)."""
    files = ", ".join(dirty[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
    return f"the checkout has uncommitted changes ({files}); commit or stash them"


async def dirty_checkout(cwd: str | Path) -> list[str]:
    """The tracked files with uncommitted changes when ``cwd`` is in a git repository with a commit, which the fan-out
    refuses for them; [] for a clean one, and for a directory without a repository or a commit (those run in place)."""
    repo = await wt_mod.repo_root(cwd)
    if repo is None or not await wt_mod.head_sha(cwd):
        return []
    return await wt_mod.uncommitted(repo)


def parse_verdict(text: str) -> bool | None:
    """True = pass, False = fail, None = the reply does not end with a verdict."""
    m = VERDICT_RE.search(text or "")
    return None if m is None else m.group(1).lower() == "pass"


@dataclass
class Subtask:
    id: str
    title: str
    state: str = "queued"
    attempts: int = 0
    handles: list[Handle] = field(default_factory=list)
    detail: str = ""
    review: str = ""
    worktree: wt_mod.Worktree | None = None
    #: the worker ended without a change: its branch holds nothing to keep or to point anyone at
    empty: bool = False
    #: agents of the budget already taken for this subtask's worker and first reviewer (see FanoutExecutor.run), and
    #: for its rework and the re-review once it is sent back (see _reserve_rework)
    prepaid: int = 0

    def row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "attempts": self.attempts,
            "detail": self.detail[:300],
        }


@dataclass
class FanoutResult:
    fanout_id: str
    subtasks: list[Subtask]
    tests: str = "none"  # pass | fail | none
    test_command: str = ""
    stopped: str = ""  # budget message
    stopped_kind: str = ""  # "agents" | "tokens" (see BudgetStop.kind)
    tests_passed: int = 0  # merged subtasks whose merged result passed the tests
    tests_failed: int = 0  # merge attempts rejected because the tests failed
    tested_in: str = ""  # the directory the merged result was tested in (the repository root)

    def by_state(self, *states: str) -> list[Subtask]:
        return [s for s in self.subtasks if s.state in states]

    @property
    def ok(self) -> bool:
        return all(s.state == "merged" for s in self.subtasks) and not self.stopped

    def summary(self) -> str:
        tests = f" (tests: {self.tests} via {self.test_command})" if self.test_command else ""
        lines = [f"Fan-out finished: {len(self.by_state('merged'))}/{len(self.subtasks)} subtasks merged{tests}."]
        for s in self.subtasks:
            extra = f" — {s.detail}" if s.detail and s.state != "merged" else ""
            lines.append(f"- [{s.state}] {s.title}{extra}")
        if self.stopped:
            lines.append(f"Stopped early: {self.stopped}")
        return "\n".join(lines)

    def unresolved_lines(self) -> list[str]:
        """One line per subtask that was not merged: its state, why, and the branch that keeps its work."""
        out = []
        for s in self.by_state("escalated", "failed", "skipped", "queued", "running", "retrying"):
            where = f"; its work is on branch {s.worktree.branch}" if s.worktree is not None and not s.empty else ""
            out.append(f"- [{s.state}] {s.title}{' — ' + s.detail if s.detail else ''}{where}")
        return out

    def escalation_prompt(self, task: str) -> str:
        bad = self.by_state("escalated", "failed", "skipped")
        parts = [f"The task was split into parallel subtasks. {self.summary()}", ""]
        for s in bad:
            parts.append(f"Subtask '{s.title}' needs you ({s.state}): {s.detail}")
            if s.worktree is not None and not s.empty:
                parts.append(f"  Its work is on branch {s.worktree.branch}.")
        parts.append(f"\nFinish the remaining work yourself so that the original task is complete: {task}")
        return "\n".join(parts)


class FanoutExecutor:
    def __init__(self, server: Any) -> None:
        self.server = server

    @property
    def cfg(self) -> dict[str, Any]:
        return autonomy_cfg(self.server.config)["fanout"]

    def applies(self, session: Any, gate: Any) -> list[str]:
        """The subtasks to fan out, or [] when this turn should just run normally."""
        v = getattr(gate, "verdict", None)
        if not self.cfg["enabled"] or v is None or not v.fanout_candidate or not gate.plan:
            return []
        if session.background or not v.parallelizable:
            return []
        if session_limits(self.server.config, session).dynamic and len(top_level_steps(gate.plan)) >= 2:
            # a plan with steps says what depends on what: when they come down to one unit this is one piece of work,
            # not a case for the classifier's guess or the older line-by-line reading
            return self.size_from_plan(session, gate.plan, v.scope)
        return extract_subtasks(v, gate.plan)

    def size_from_plan(self, session: Any, plan: str, scope: str | None) -> list[str]:
        """The subtasks for a plan, sized to the task: the plan's own top-level steps (the strong planner's plan is
        better evidence than the cheap classifier's suggestions), grouped into independent units and packed to as many
        workers as the scope can keep busy. [] when the plan has fewer than two independent units."""
        units = [unit_text(u) for u in group_steps(top_level_steps(plan))]
        if len(units) < 2:
            return []
        limits = session_limits(self.server.config, session)
        io_heavy = any(IO_HEAVY_RE.search(u) for u in units)
        rel = session.reliability
        gov = rel.governor if rel is not None and rel.governor is not None else Governor()
        cap = gov.agent_cap(limits.fanout_width, io_heavy=io_heavy, ceiling=limits.max_parallel)
        sized = size_agents(units, whole="", left=None, hold=0, width_cap=cap, scope=scope)
        return sized.subtasks if sized.workers >= 2 else []

    # ── the run ──

    async def run(
        self,
        session: Any,
        task: str,
        plan: str,
        subtasks: list[str],
        *,
        budget: AgentBudget | None = None,
        require_tests: bool | None = None,
        width: int | None = None,
    ) -> FanoutResult | None:
        cwd = Path(session.perms.cwd)
        limits = session_limits(self.server.config, session)
        if budget is None and limits.dynamic:  # plan-first had no spend bound: what each worker can use (see sizing)
            budget = AgentBudget(max_agents=WORKER_AGENTS * len(subtasks))
        if await wt_mod.repo_root(cwd) is None or not await wt_mod.head_sha(cwd):
            session.emit(
                "notification.show",
                {"text": "fan-out skipped: not a git repository with a commit", "level": "info", "kind": "fanout"},
            )
            return None
        cfg = self.cfg
        repo = await wt_mod.repo_root(cwd)
        assert repo is not None
        if dirty := await wt_mod.uncommitted(repo):  # workers start from HEAD and every merge lands in this checkout
            session.emit(
                "notification.show",
                {"text": f"fan-out skipped: {dirty_text(dirty)}", "level": "info", "kind": "fanout"},
            )
            return None
        test_cmd = (cfg.get("test_command") or detect_test_command(repo)) or ""
        need_tests = cfg["require_tests"] if require_tests is None else require_tests
        io_heavy = any(IO_HEAVY_RE.search(s) for s in subtasks)
        gov = session.reliability.governor if session.reliability and session.reliability.governor else Governor()
        cap = gov.agent_cap(limits.fanout_width, io_heavy=io_heavy, ceiling=limits.max_parallel)
        if width is not None:  # a sized run (see k3code.autonomy.sizing) asks for no more than it needs
            cap = max(1, min(cap, width))
        shrink = gov.pressure_shrink() if cap > 1 else 0  # a loaded machine runs one child fewer at a time
        cap -= shrink
        fid = f"fo-{uuid.uuid4().hex[:6]}"
        result = FanoutResult(
            fid,
            [Subtask(f"t{i + 1}", t) for i, t in enumerate(subtasks)],
            test_command=test_cmd if need_tests else "",
            tested_in=str(repo),
        )
        sid = session.session_id
        session.emit(
            "fanout.plan",
            {
                "session_id": sid,
                "fanout_id": fid,
                "max_parallel": cap,
                "pressure_shrink": shrink,
                "io_heavy": io_heavy,
                "require_tests": need_tests,
                "test_command": test_cmd,
                "subtasks": [s.row() for s in result.subtasks],
            },
            importance="essential",
        )
        sem = asyncio.Semaphore(cap)
        merge_lock = asyncio.Lock()
        ctx = _Ctx(
            session,
            task,
            plan,
            result,
            budget,
            test_cmd if need_tests else "",
            float(cfg["test_timeout"]),
            sem,
            merge_lock,
            self,
        )
        self._prepay(ctx)
        try:
            await asyncio.gather(*(self._one(ctx, st) for st in result.subtasks))
        except asyncio.CancelledError:
            self.server.subagents.interrupt_session(sid)
            raise
        finally:
            await self._cleanup(result)
        if need_tests and test_cmd:
            escalated_on_tests = any(s.state == "escalated" and "tests failed" in s.detail for s in result.subtasks)
            result.tests = "fail" if escalated_on_tests else "pass" if result.tests_passed else "none"
        session.emit(
            "fanout.done",
            {
                "session_id": sid,
                "fanout_id": fid,
                "ok": result.ok,
                "merged": len(result.by_state("merged")),
                "escalated": len(result.by_state("escalated", "failed", "skipped")),
                "total": len(result.subtasks),
                "tests": result.tests,
                "summary": result.summary(),
            },
            importance="essential",
        )
        return result

    def progress(self, ctx: _Ctx, st: Subtask, state: str, detail: str = "") -> None:
        st.state, st.detail = state, detail or st.detail
        done = len(ctx.result.by_state("merged", "escalated", "failed", "skipped"))
        ctx.session.emit(
            "fanout.progress",
            {
                "session_id": ctx.session.session_id,
                "fanout_id": ctx.result.fanout_id,
                "subtask_id": st.id,
                "title": st.title,
                "state": state,
                "detail": detail[:300],
                "done": done,
                "total": len(ctx.result.subtasks),
                "subagent_ids": [h.id for h in st.handles],
            },
        )

    def _prepay(self, ctx: _Ctx) -> None:
        """Take the worker and the first reviewer of every subtask from the budget now, in plan order. A rework or a
        re-review draws on what is left after that, so one rejected result can never use up the agents that a sibling's
        reviewer needs (at the default 12 it did: nothing merged and the review panel never ran). A subtask the budget
        cannot pay for is skipped before it starts."""
        budget = ctx.budget
        if budget is None:
            return
        for st in ctx.result.subtasks:
            try:
                budget.take_agent()
                st.prepaid = 1
                budget.take_agent()
                st.prepaid = 2
            except BudgetStop as e:
                budget.refund(st.prepaid)
                st.prepaid = 0
                ctx.result.stopped = ctx.result.stopped or str(e)
                ctx.result.stopped_kind = ctx.result.stopped_kind or e.kind
                for rest in ctx.result.subtasks[ctx.result.subtasks.index(st) :]:
                    rest.state, rest.detail = "skipped", str(e)
                return

    def _take(self, ctx: _Ctx, st: Subtask) -> None:
        """The agent for the next child of ``st``: its prepaid one, else a new one from the budget."""
        if ctx.budget is None:
            return
        if st.prepaid > 0:
            st.prepaid -= 1
        else:
            ctx.budget.take_agent()

    def _reserve_rework(self, ctx: _Ctx, st: Subtask) -> str:
        """Take the agents of a rework and of its re-review now: "" when both are paid for, else why they are not (and
        nothing is taken). A rework is new work that merges only after its own pass, so one whose re-review the budget
        refuses spends an agent and merges nothing. Check and take happen together, with no await between them: reworks
        running side by side cannot both count the same free agent. Agents it does not use are refunded by _one."""
        budget = ctx.budget
        if budget is None:
            return ""
        need = max(0, 2 - st.prepaid)
        free = budget.left()
        if free is not None and free < need:
            held = f", {budget.reserved} held back for the review" if budget.reserved else ""
            return (
                f"agent budget exhausted ({budget.agents}/{budget.max_agents} agents{held}): "
                f"a rework and its re-review need {need} more"
            )
        for _ in range(need):
            budget.take_agent()
            st.prepaid += 1
        return ""

    async def _spawn(self, ctx: _Ctx, st: Subtask, agent_type: str, prompt: str, description: str, **kw: Any) -> Handle:
        self._take(ctx, st)
        mgr = self.server.subagents
        idx = int(st.id[1:]) - 1
        try:
            h = mgr.spawn(
                ctx.session,
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                index=idx,
                count=len(ctx.result.subtasks),
                **kw,
            )
        except BaseException:
            if ctx.budget is not None:  # nothing started: the agent is not spent
                ctx.budget.refund(1)
            raise
        st.handles.append(h)
        if self.cfg.get("panes"):  # k3 panes: one read-only pane per child (the pane's process opens it)
            ctx.session.emit("pane.open", {"subagent_id": h.id, "name": f"fan {st.id} {st.title}"[:40]})
        await mgr.wait(h)
        if ctx.budget is not None:
            ctx.budget.charge(h)
        return h

    def worker_prompt(self, ctx: _Ctx, st: Subtask) -> str:
        files = parse_plan(ctx.plan).get("Files", "").strip()
        others = "\n".join(f"- {o.title}" for o in ctx.result.subtasks if o is not st)
        return (
            f"You are one of {len(ctx.result.subtasks)} workers running in parallel on this task:\n{ctx.task}\n\n"
            f"YOUR subtask ({st.id}): {st.title}\n\nShared plan:\n{ctx.plan}\n\n"
            + (f"Relevant files:\n{files}\n\n" if files else "")
            + f"Other workers handle these; do not do them:\n{others}\n\n"
            "Work only on your subtask, in your own worktree. Run the relevant tests if the project has them. "
            "Finish with a short report of what changed."
        )

    async def _one(self, ctx: _Ctx, st: Subtask) -> None:
        if st.state == "skipped":  # the budget could not pay for it (see _prepay)
            self.progress(ctx, st, "skipped", st.detail)
            return
        try:
            async with ctx.sem:
                self.progress(ctx, st, "running")
                h = await self._spawn(
                    ctx,
                    st,
                    "worker",
                    self.worker_prompt(ctx, st),
                    st.title[:60],
                    isolation="worktree",
                    auto_merge=False,
                )
                if h.status != "completed" or h.worktree is None:
                    self.progress(
                        ctx,
                        st,
                        "failed",
                        h.error or f"worker {h.status}" + ("" if h.worktree is not None else " (no worktree)"),
                    )
                    return
                st.worktree = h.worktree
                if h.merge == "no_changes":
                    st.empty = True
                    self.progress(ctx, st, "failed", "the worker made no changes")
                    return
                err = await self._review(ctx, st, h)
                if err:  # sent back once, then escalated
                    if why := self._reserve_rework(ctx, st):  # nothing is spent on a rework that could not be reviewed
                        self.progress(ctx, st, "escalated", f"{why}; the reviewer rejected the result:\n{err}")
                        return
                    st.attempts += 1
                    self.progress(ctx, st, "retrying", err)
                    h = await self._rework(ctx, st, f"A reviewer rejected your result:\n{err}")
                    err = await self._review(ctx, st, h) if h.status == "completed" else h.error or "rework failed"
                    if err:
                        self.progress(ctx, st, "escalated", f"review failed after a retry: {err}")
                        return
            await self._merge(ctx, st)
        except BudgetStop as e:
            ctx.result.stopped = ctx.result.stopped or str(e)
            ctx.result.stopped_kind = ctx.result.stopped_kind or e.kind
            self.progress(ctx, st, "skipped", str(e))
        except ReviewUnavailable as e:  # reworking would not help: the work was never judged
            self.progress(ctx, st, "escalated", f"no review verdict: {e}")
        except Exception as e:  # noqa: BLE001 - one subtask must not take the others down
            logger.exception("fan-out subtask %s crashed", st.id)
            self.progress(ctx, st, "failed", f"{type(e).__name__}: {e}")
        finally:
            if ctx.budget is not None and st.prepaid:  # a reviewer that never ran: its agent goes back to the pool
                ctx.budget.refund(st.prepaid)
                st.prepaid = 0

    def review_prompt(self, ctx: _Ctx, st: Subtask, worker: Handle) -> str:
        verification = parse_plan(ctx.plan).get("Verification", "").strip()
        diff = worker.diff or ""
        wt = worker.worktree or st.worktree
        where = (
            f"The change is not in your working directory: it is on branch {wt.branch} in the worktree {wt.path} "
            f"(`git -C {wt.path} diff {wt.base}` shows all of it). Read the files there.\n"
            if wt is not None
            else ""
        )
        cut = (
            f"\nThe diff above is CUT at {REVIEW_DIFF_CHARS} of {len(diff)} characters: read the rest in the worktree "
            "before you give a verdict.\n"
            if len(diff) > REVIEW_DIFF_CHARS
            else ""
        )
        return (
            f"Review one worker's result against its acceptance criteria.\n\nSubtask: {st.title}\n"
            f"Acceptance criteria: the subtask is fully done, correct and does not touch unrelated code."
            + (f" Plan verification: {verification}" if verification else "")
            + f"\n{where}\nWorker report:\n{worker.result}\n\nDiff ({worker.diff_stat or 'no stat'}):\n```diff\n"
            f"{diff[:REVIEW_DIFF_CHARS]}\n```\n{cut}End with 'VERDICT: pass' or 'VERDICT: fail'."
        )

    async def _review(self, ctx: _Ctx, st: Subtask, worker: Handle) -> str:
        """Reviewer child against the acceptance criteria. Returns "" on an explicit 'VERDICT: pass', else the
        findings. A reviewer that fails, is stopped or ends without a verdict is run once more; when none gives a
        verdict this raises ReviewUnavailable: the gate never opens without a pass."""
        self.progress(ctx, st, "reviewing")
        prompt = self.review_prompt(ctx, st, worker)
        problem = ""
        for _ in range(REVIEW_ATTEMPTS):
            r = await self._spawn(ctx, st, "reviewer", prompt, f"review: {st.title}"[:60])
            st.review = r.result or ""
            if r.status != "completed":
                if ctx.budget is not None:  # cut off by the token cap: no rerun can pay for the missing verdict
                    ctx.budget.stop_if_capped(r)
                problem = f"the reviewer {r.status}" + (f": {r.error}" if r.error else "")
                continue
            verdict = parse_verdict(r.result)
            if verdict is not None:
                return "" if verdict else r.result
            problem = "the reviewer ended without a 'VERDICT: pass' or 'VERDICT: fail' line"
        raise ReviewUnavailable(problem)

    async def _rework(self, ctx: _Ctx, st: Subtask, problem: str) -> Handle:
        assert st.worktree is not None
        prompt = (
            f"{self.worker_prompt(ctx, st)}\n\n--- REWORK ---\nYou already worked on this in your worktree "
            f"(branch {st.worktree.branch}). Fix this problem and finish:\n{problem}"
        )
        return await self._spawn(
            ctx,
            st,
            "worker",
            prompt,
            f"fix: {st.title}"[:60],
            isolation="worktree",
            auto_merge=False,
            worktree=st.worktree,
        )

    async def _merge(self, ctx: _Ctx, st: Subtask) -> None:
        wt = st.worktree
        assert wt is not None
        while True:
            async with ctx.merge_lock:  # one merge + test run at a time
                self.progress(ctx, st, "merging")
                ok, out = await wt_mod.merge_trial(wt)
                error, blocked = "", not ok and out.startswith(wt_mod.BLOCKED)
                if blocked:
                    error = out[:1500]
                elif not ok:
                    error = f"merge conflict with what was already merged:\n{out[-1500:]}"
                elif ctx.test_cmd:
                    self.progress(ctx, st, "testing")
                    passed, tail = await run_tests(ctx.test_cmd, wt.repo, ctx.test_timeout)
                    if passed:
                        ctx.result.tests_passed += 1
                    else:
                        ctx.result.tests_failed += 1
                        await wt_mod.abort_merge(wt)
                        error = f"tests failed after merging ({ctx.test_cmd}):\n{tail}"
                if not error:
                    committed, cout = await wt_mod.commit_merge(wt)
                    if committed:
                        self.progress(ctx, st, "merged")
                        return
                    await wt_mod.abort_merge(wt)
                    error = f"could not commit the merge: {cout}"
                parent_head = await wt_mod.head_sha(wt.repo)
            if st.attempts >= 1 or blocked:  # the child cannot clean the user's checkout: no rework for that
                self.progress(ctx, st, "escalated", error)
                return
            if why := self._reserve_rework(ctx, st):  # nothing is spent on a rework that could not be reviewed
                self.progress(ctx, st, "escalated", f"{error}\n(no rework: {why})")
                return
            st.attempts += 1
            self.progress(ctx, st, "retrying", error)
            clean, mout = await wt_mod.bring_parent_into(wt, parent_head)
            problem = error
            if not clean:
                files = await wt_mod.conflicted_files(wt.path)
                problem += (
                    f"\n\nI merged the current main state into your worktree and it conflicts in: "
                    f"{', '.join(files) or '(see git status)'}. Resolve the conflict markers, keep both "
                    "sides' intent, and save the files."
                )
            async with ctx.sem:  # the rework and its review are children like the others: they count against the width
                h = await self._rework(ctx, st, problem)
                if h.status != "completed":
                    self.progress(ctx, st, "escalated", f"{error}\n(retry {h.status}: {h.error})")
                    return
                err = await self._review(ctx, st, h)  # the rework is new work: it merges only after its own pass
            if err:
                self.progress(ctx, st, "escalated", f"{error}\nThe reviewer rejected the fix:\n{err}")
                return

    async def _cleanup(self, result: FanoutResult) -> None:
        for st in result.subtasks:
            if st.worktree is None:
                continue
            try:
                await wt_mod.remove(st.worktree, keep_branch=st.state != "merged" and not st.empty)
            except Exception:  # noqa: BLE001
                logger.debug("worktree cleanup failed for %s", st.id, exc_info=True)


@dataclass
class _Ctx:
    session: Any
    task: str
    plan: str
    result: FanoutResult
    budget: AgentBudget | None
    test_cmd: str
    test_timeout: float
    sem: asyncio.Semaphore
    merge_lock: asyncio.Lock
    executor: FanoutExecutor
