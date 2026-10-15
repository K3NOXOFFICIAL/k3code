# Vendored from hermes-agent@4127d78da84b1eee105f298979cc57cc7457f98d:hermes_cli/kanban_swarm.py (MIT)
# Design port only (decompose -> parallel workers -> review -> sequential merge); no code copied. See VENDOR.toml.
"""Automatic fan-out: run the parallelizable subtasks of a large plan as worktree children.

Per subtask: worker child (own worktree) -> reviewer child against the acceptance criteria -> sequential
merge into the parent checkout with the project's tests run on the merged result before it is committed.
A failing review, merge or test goes back to that child ONCE with the error; after that it is escalated to
the parent (the branch is kept, the parent model gets the details).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.autonomy import DEFAULTS, autonomy_cfg
from k3code.autonomy.plan_first import parse_plan
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
#: Reviewer runs per review before it counts as unavailable (a crash, a stop or a reply without a verdict).
REVIEW_ATTEMPTS = 2
#: Seconds the sandboxed check that a detected test command's program is there may take.
PROBE_TIMEOUT = 30


class ReviewUnavailable(Exception):
    """No reviewer returned a verdict: the gate stays shut and the subtask goes to the parent."""


class StoppedByBudget(BudgetStop):
    """A child was interrupted while it ran because the fan-out's token cap was crossed (its subtask: ``stopped``)."""


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
    """pytest, npm test, go test or cargo test, by marker files.

    pytest runs with the project's ``.venv`` interpreter when there is one, else ``python3``: the tests run in the
    sandbox, where no venv is active and stock Ubuntu has no ``python``.
    """
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
        python = ".venv/bin/python" if (root / ".venv" / "bin" / "python").is_file() else "python3"
        return f"{python} -m pytest -q -x"
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


async def preflight(repo: str | Path, test_cmd: str, *, detected: bool) -> str:
    """Why fan-out cannot work here, or "" when it can.

    Without a usable bwrap every child's bash call and every merge's test run is refused. A detected test command
    whose program is missing in the sandbox (``$HOME`` is hidden there, so ``~/.local/bin`` is gone) or, for pytest,
    cannot import it, fails every merge. A configured ``test_command`` is the user's choice and is not probed.
    """
    from k3code.reliability import sandbox

    if not await asyncio.to_thread(sandbox.usable):
        return "bubblewrap is unusable here, so sub-agents could not run commands or test a merge; see /doctor"
    if not (test_cmd and detected):
        return ""
    exe = shlex.quote(shlex.split(test_cmd)[0])
    probe = f"{exe} -c 'import pytest'" if " -m pytest" in test_cmd else f"command -v {exe}"
    ok, out = await run_tests(probe, repo, PROBE_TIMEOUT)
    if ok:
        return ""
    reason = (out.strip().splitlines() or [f"{exe}: not found"])[-1]
    return (
        f"the detected test command `{test_cmd}` cannot run in the sandbox ({reason}); "
        "set autonomy.fanout.test_command to one that can"
    )


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
    reserved: int = 0  # reviewer slots its running worker keeps in the agent budget

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
    tests_passed: int = 0  # merged subtasks whose merged result passed the tests
    tests_failed: int = 0  # merge attempts rejected because the tests failed

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

    def escalation_prompt(self, task: str) -> str:
        bad = self.by_state("escalated", "failed", "skipped", "stopped")
        parts = [f"The task was split into parallel subtasks. {self.summary()}", ""]
        for s in bad:
            parts.append(f"Subtask '{s.title}' needs you ({s.state}): {s.detail}")
            if s.worktree is not None:
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
        return extract_subtasks(v, gate.plan)

    def budget(self, n_subtasks: int) -> AgentBudget:
        """The budget of an auto fan-out: ``autonomy.fanout.max_tokens`` (0 = no cap; default 1,000,000) and
        ``max_agents`` (0 = 4 agents per subtask, at most 16; a worker only starts while a reviewer slot is kept for
        it and for every worker already running). A value that is not a non-negative whole number (``"500k"``, ``-1``,
        ``null``, ``true``) counts as unset, so a typo never removes the cap. /ultracode builds its own, shared with
        its planners."""
        max_agents = self._count("max_agents", 0) or min(4 * n_subtasks, 16)
        return AgentBudget(max_agents, self._count("max_tokens", DEFAULTS["fanout"]["max_tokens"]) or None)

    def _count(self, key: str, default: int) -> int:
        v = self.cfg[key]
        if isinstance(v, float) and v.is_integer():  # YAML reads ``1.0e+6`` as a float
            v = int(v)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return v
        logger.warning("autonomy.fanout.%s=%r is not a non-negative whole number; using %d", key, v, default)
        return default

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
    ) -> FanoutResult | None:
        cwd = Path(session.perms.cwd)
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
            files = ", ".join(dirty[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
            session.emit(
                "notification.show",
                {
                    "text": f"fan-out skipped: the checkout has uncommitted changes ({files}); commit or stash them",
                    "level": "info",
                    "kind": "fanout",
                },
            )
            return None
        test_cmd = (cfg.get("test_command") or detect_test_command(repo)) or ""
        need_tests = cfg["require_tests"] if require_tests is None else require_tests
        if why := await preflight(repo, test_cmd if need_tests else "", detected=not cfg.get("test_command")):
            session.emit("notification.show", {"text": f"fan-out skipped: {why}", "level": "warning", "kind": "fanout"})
            return None
        io_heavy = any(IO_HEAVY_RE.search(s) for s in subtasks)
        gov = session.reliability.governor if session.reliability and session.reliability.governor else Governor()
        cap = gov.agent_cap(int(cfg["max_parallel"]), io_heavy=io_heavy)
        fid = f"fo-{uuid.uuid4().hex[:6]}"
        result = FanoutResult(
            fid, [Subtask(f"t{i + 1}", t) for i, t in enumerate(subtasks)], test_command=test_cmd if need_tests else ""
        )
        sid = session.session_id
        session.emit(
            "fanout.plan",
            {
                "session_id": sid,
                "fanout_id": fid,
                "max_parallel": cap,
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
                "escalated": len(result.by_state("escalated", "failed", "skipped", "stopped")),
                "total": len(result.subtasks),
                "tests": result.tests,
                "summary": result.summary(),
            },
            importance="essential",
        )
        return result

    def progress(self, ctx: _Ctx, st: Subtask, state: str, detail: str = "") -> None:
        st.state, st.detail = state, detail or st.detail
        done = len(ctx.result.by_state("merged", "escalated", "failed", "skipped", "stopped"))
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

    async def _spawn(self, ctx: _Ctx, st: Subtask, agent_type: str, prompt: str, description: str, **kw: Any) -> Handle:
        # the parent's session/day budget (reliability.session_tokens, day_tokens, ...) already counts every finished
        # child (runner charges it): once it is spent, no further child starts
        if (rel := getattr(ctx.session, "reliability", None)) is not None and (err := rel.check_budgets()) is not None:
            raise BudgetStop(str(err))
        if ctx.budget is not None:
            if ctx.budget.max_agents is not None and agent_type in ("worker", "reviewer"):
                # a worker's result is useless unmerged-and-unreviewed: it starts only while a slot is left for its
                # reviewer on top of the slots kept for the reviewers of the workers already running. A reviewer uses
                # its worker's slot; a second try of a reviewer has none and must not eat another worker's
                ctx.budget.check()
                own = st.reserved if agent_type == "reviewer" else 0
                need = 2 if agent_type == "worker" else 1 - own
                free = ctx.budget.max_agents - ctx.budget.agents - ctx.reserved
                if free < need:
                    kept = f", {ctx.reserved} kept for reviewers" if ctx.reserved else ""
                    b = ctx.budget
                    raise BudgetStop(f"agent budget exhausted ({b.agents}/{b.max_agents} agents{kept})")
            ctx.budget.take_agent()
            if agent_type == "worker":
                st.reserved, ctx.reserved = st.reserved + 1, ctx.reserved + 1
            elif agent_type == "reviewer":
                self._release(ctx, st)
        mgr = self.server.subagents
        idx = int(st.id[1:]) - 1
        h = mgr.spawn(
            ctx.session,
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            index=idx,
            count=len(ctx.result.subtasks),
            **kw,
        )
        st.handles.append(h)
        if self.cfg.get("panes"):  # k3 panes: one read-only pane per child (the pane's process opens it)
            ctx.session.emit("pane.open", {"subagent_id": h.id, "name": f"fan {st.id} {st.title}"[:40]})
        if ctx.budget is not None:
            # the cap was checked only before a child started and charged only after it ended, so up to
            # max_parallel running children overshot it by a whole run each. Its tokens now count while it runs, and
            # after every model call of any child the running ones are stopped once the cap is crossed.
            ctx.budget.track(h)
            h.on_usage = lambda child, final: self._stop_over_budget(ctx, child if final else None)
        try:
            await mgr.wait(h)
        finally:
            if ctx.budget is not None:
                ctx.budget.charge(h)
        if (why := ctx.stopped.get(h.id)) is not None:
            if st.worktree is None and h.worktree is not None and h.merge == "pending":
                st.worktree = h.worktree  # its partial work is on its branch: the parent is told where
            raise StoppedByBudget(why)
        return h

    def _stop_over_budget(self, ctx: _Ctx, finishing: Handle | None = None) -> None:
        """Called after every model call of a running child: once the token cap is crossed (the finished children
        plus what the running ones used so far), interrupt every running child and note why, so its subtask ends as
        ``stopped`` by the budget (not as a failed child) and the summary says so.

        ``finishing`` is the child whose call this was when that call was its final answer: it makes no further
        call, so stopping it would save nothing and only throw away a finished result (a reviewer's verdict)."""
        budget = ctx.budget
        if budget is None or not (why := budget.over()):
            return
        for h in list(budget.running.values()):
            # once only: a second cancel would cut the child's own cleanup short (committing its partial work). A
            # child with no loop yet is still setting up (its worktree): cancelling a `git worktree add` midway could
            # leave a broken checkout, so it is stopped after its own first call instead.
            if h is finishing or h.id in ctx.stopped or h.loop is None:
                continue
            if not self.server.subagents.interrupt(h.id):
                continue
            ctx.stopped[h.id] = f"{why}; stopped while running"
            logger.info("fan-out %s: %s; stopped sub-agent %s", ctx.result.fanout_id, why, h.id)

    @staticmethod
    def _release(ctx: _Ctx, st: Subtask) -> None:
        """Give back the reviewer slot a worker kept (its reviewer started, or the worker has nothing to review)."""
        ctx.reserved -= st.reserved
        st.reserved = 0

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
                    self.progress(ctx, st, "failed", "the worker made no changes")
                    return
                err = await self._review(ctx, st, h)
                if err:  # sent back once, then escalated
                    st.attempts += 1
                    self.progress(ctx, st, "retrying", err)
                    h = await self._rework(ctx, st, f"A reviewer rejected your result:\n{err}")
                    err = await self._review(ctx, st, h) if h.status == "completed" else h.error or "rework failed"
                    if err:
                        self.progress(ctx, st, "escalated", f"review failed after a retry: {err}")
                        return
            await self._merge(ctx, st)
        except BudgetStop as e:  # a child the budget stopped while it ran, or a child it never let start
            ctx.result.stopped = ctx.result.stopped or str(e)
            self.progress(ctx, st, "stopped" if isinstance(e, StoppedByBudget) else "skipped", str(e))
        except ReviewUnavailable as e:  # reworking would not help: the work was never judged
            self.progress(ctx, st, "escalated", f"no review verdict: {e}")
        except Exception as e:  # noqa: BLE001 - one subtask must not take the others down
            logger.exception("fan-out subtask %s crashed", st.id)
            self.progress(ctx, st, "failed", f"{type(e).__name__}: {e}")
        finally:
            self._release(ctx, st)  # a worker that failed or changed nothing never reaches its reviewer

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
                await wt_mod.remove(st.worktree, keep_branch=st.state != "merged")
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
    reserved: int = 0  # reviewer slots kept for the workers that started and have not been reviewed yet
    #: why each child the token cap stopped while it ran was stopped, by handle id (see _stop_over_budget)
    stopped: dict[str, str] = field(default_factory=dict)
