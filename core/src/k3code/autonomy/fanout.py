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
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.autonomy import autonomy_cfg
from k3code.autonomy.plan_first import parse_plan
from k3code.reliability.governor import Governor
from k3code.subagents import worktree as wt_mod
from k3code.subagents.budget import AgentBudget, BudgetStop
from k3code.subagents.runner import Handle

logger = logging.getLogger(__name__)

MAX_SUBTASKS = 8
IO_HEAVY_RE = re.compile(r"\b(install|build|compile|download|bundle|docker|index|crawl|migrat\w+)\b", re.I)
VERDICT_RE = re.compile(r"VERDICT:\s*(pass|fail)", re.I)


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
    """Run the test command; pytest's "no tests collected" (5) counts as a pass."""
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"tests timed out after {timeout:.0f}s"
    text = out.decode("utf-8", "replace")
    ok = proc.returncode == 0 or (proc.returncode == 5 and "pytest" in cmd)
    return ok, text[-4000:]


def parse_verdict(text: str) -> bool | None:
    """True = pass, False = fail, None = the reviewer gave no verdict."""
    found = VERDICT_RE.findall(text or "")
    return None if not found else found[-1].lower() == "pass"


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

    def row(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "state": self.state, "attempts": self.attempts,
                "detail": self.detail[:300]}


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
        bad = self.by_state("escalated", "failed", "skipped")
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

    # ── the run ──

    async def run(
        self, session: Any, task: str, plan: str, subtasks: list[str], *, budget: AgentBudget | None = None,
        require_tests: bool | None = None,
    ) -> FanoutResult | None:
        cwd = Path(session.perms.cwd)
        if await wt_mod.repo_root(cwd) is None or not await wt_mod.head_sha(cwd):
            session.emit("notification.show", {"text": "fan-out skipped: not a git repository with a commit",
                                               "level": "info", "kind": "fanout"})
            return None
        cfg = self.cfg
        repo = await wt_mod.repo_root(cwd)
        assert repo is not None
        test_cmd = (cfg.get("test_command") or detect_test_command(repo)) or ""
        need_tests = cfg["require_tests"] if require_tests is None else require_tests
        io_heavy = any(IO_HEAVY_RE.search(s) for s in subtasks)
        gov = session.reliability.governor if session.reliability and session.reliability.governor else Governor()
        cap = gov.agent_cap(int(cfg["max_parallel"]), io_heavy=io_heavy)
        fid = f"fo-{uuid.uuid4().hex[:6]}"
        result = FanoutResult(fid, [Subtask(f"t{i + 1}", t) for i, t in enumerate(subtasks)],
                              test_command=test_cmd if need_tests else "")
        sid = session.session_id
        session.emit("fanout.plan", {
            "session_id": sid, "fanout_id": fid, "max_parallel": cap, "io_heavy": io_heavy,
            "require_tests": need_tests, "test_command": test_cmd,
            "subtasks": [s.row() for s in result.subtasks],
        }, importance="essential")
        sem = asyncio.Semaphore(cap)
        merge_lock = asyncio.Lock()
        ctx = _Ctx(session, task, plan, result, budget, test_cmd if need_tests else "", float(cfg["test_timeout"]),
                   sem, merge_lock, self)
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
        session.emit("fanout.done", {
            "session_id": sid, "fanout_id": fid, "ok": result.ok, "merged": len(result.by_state("merged")),
            "escalated": len(result.by_state("escalated", "failed", "skipped")), "total": len(result.subtasks),
            "tests": result.tests, "summary": result.summary(),
        }, importance="essential")
        return result

    def progress(self, ctx: _Ctx, st: Subtask, state: str, detail: str = "") -> None:
        st.state, st.detail = state, detail or st.detail
        done = len(ctx.result.by_state("merged", "escalated", "failed", "skipped"))
        ctx.session.emit("fanout.progress", {
            "session_id": ctx.session.session_id, "fanout_id": ctx.result.fanout_id, "subtask_id": st.id,
            "title": st.title, "state": state, "detail": detail[:300], "done": done,
            "total": len(ctx.result.subtasks), "subagent_ids": [h.id for h in st.handles],
        })

    async def _spawn(self, ctx: _Ctx, st: Subtask, agent_type: str, prompt: str, description: str, **kw: Any) -> Handle:
        if ctx.budget is not None:
            ctx.budget.take_agent()
        mgr = self.server.subagents
        idx = int(st.id[1:]) - 1
        h = mgr.spawn(ctx.session, description=description, prompt=prompt, agent_type=agent_type,
                      index=idx, count=len(ctx.result.subtasks), **kw)
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
        try:
            async with ctx.sem:
                self.progress(ctx, st, "running")
                h = await self._spawn(ctx, st, "worker", self.worker_prompt(ctx, st), st.title[:60],
                                      isolation="worktree", auto_merge=False)
                if h.status != "completed" or h.worktree is None:
                    self.progress(ctx, st, "failed", h.error or f"worker {h.status}" +
                                  ("" if h.worktree is not None else " (no worktree)"))
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
        except BudgetStop as e:
            ctx.result.stopped = ctx.result.stopped or str(e)
            self.progress(ctx, st, "skipped", str(e))
        except Exception as e:  # noqa: BLE001 - one subtask must not take the others down
            logger.exception("fan-out subtask %s crashed", st.id)
            self.progress(ctx, st, "failed", f"{type(e).__name__}: {e}")

    async def _review(self, ctx: _Ctx, st: Subtask, worker: Handle) -> str:
        """Reviewer child against the acceptance criteria. Returns "" on pass, else the findings."""
        self.progress(ctx, st, "reviewing")
        verification = parse_plan(ctx.plan).get("Verification", "").strip()
        prompt = (
            f"Review one worker's result against its acceptance criteria.\n\nSubtask: {st.title}\n"
            f"Acceptance criteria: the subtask is fully done, correct and does not touch unrelated code."
            + (f" Plan verification: {verification}" if verification else "")
            + f"\n\nWorker report:\n{worker.result}\n\nDiff ({worker.diff_stat or 'no stat'}):\n```diff\n"
            f"{worker.diff[:20000]}\n```\nEnd with 'VERDICT: pass' or 'VERDICT: fail'."
        )
        r = await self._spawn(ctx, st, "reviewer", prompt, f"review: {st.title}"[:60])
        st.review = r.result
        verdict = parse_verdict(r.result)
        return r.result if verdict is False else ""

    async def _rework(self, ctx: _Ctx, st: Subtask, problem: str) -> Handle:
        assert st.worktree is not None
        prompt = (f"{self.worker_prompt(ctx, st)}\n\n--- REWORK ---\nYou already worked on this in your worktree "
                  f"(branch {st.worktree.branch}). Fix this problem and finish:\n{problem}")
        return await self._spawn(ctx, st, "worker", prompt, f"fix: {st.title}"[:60], isolation="worktree",
                                 auto_merge=False, worktree=st.worktree)

    async def _merge(self, ctx: _Ctx, st: Subtask) -> None:
        wt = st.worktree
        assert wt is not None
        while True:
            async with ctx.merge_lock:  # one merge + test run at a time
                self.progress(ctx, st, "merging")
                ok, out = await wt_mod.merge_trial(wt)
                error = ""
                if not ok:
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
            if st.attempts >= 1:
                self.progress(ctx, st, "escalated", error)
                return
            st.attempts += 1
            self.progress(ctx, st, "retrying", error)
            clean, mout = await wt_mod.bring_parent_into(wt, parent_head)
            problem = error
            if not clean:
                files = await wt_mod.conflicted_files(wt.path)
                problem += (f"\n\nI merged the current main state into your worktree and it conflicts in: "
                            f"{', '.join(files) or '(see git status)'}. Resolve the conflict markers, keep both "
                            "sides' intent, and save the files.")
            h = await self._rework(ctx, st, problem)
            if h.status != "completed":
                self.progress(ctx, st, "escalated", f"{error}\n(retry {h.status}: {h.error})")
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
