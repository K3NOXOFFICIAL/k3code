"""Plan-first: scope-gate every new task, run a read-only strong-tier planning turn when warranted."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.agent.loop import AgentLoop
from k3code.autonomy import advisor as advisor_mod
from k3code.autonomy import autonomy_cfg, scope
from k3code.autonomy.proposals import ProposalStore, propose
from k3code.autonomy.scope import ScopeLog, ScopeVerdict
from k3code.permissions import PermissionMode
from k3code.permissions.state import PermissionState
from k3code.routing.tiers import TaskKind, Tier

logger = logging.getLogger(__name__)

PLAN_SECTIONS = ("Goal", "Steps", "Files", "Risks", "Verification", "Estimate")

PLAN_ADDENDUM = (
    "\n\nYou are in PLANNING mode: read-only. Explore the repo as needed, then call exit_plan ONCE with a "
    "plan in markdown with exactly these sections: "
    + ", ".join(f"## {s}" for s in PLAN_SECTIONS)
    + ". Estimate = rough size/time. Do not implement anything."
)


@dataclass
class GateResult:
    """What ``_run_turn`` should do next."""

    prompt: str
    proceed: bool = True
    verdict: ScopeVerdict | None = None
    plan: str = ""
    message: str = ""
    scope_hash: str = ""


@dataclass
class _Captured:
    plan: str = ""
    approved: bool = False
    auto: bool = False
    mode: str | None = None
    asked: int = field(default=0)


def parse_plan(text: str) -> dict[str, str]:
    """Split a markdown plan into its known sections (missing ones are simply absent)."""
    sections: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^\s*#{1,4}\s*(\w+)", line)
        if m and m.group(1).capitalize() in PLAN_SECTIONS:
            current = m.group(1).capitalize()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return {k: v.strip() for k, v in sections.items()}


def learning_project(session: Any) -> str:
    from k3code.learning.decisions import project_id

    return project_id(session.stored.cwd or ".")


class PlanFirst:
    """Scope gate + planning turn + proposer/advisor hooks, bound to a GatewayServer."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.proposals = ProposalStore(server._home())
        self.scope_log = ScopeLog(server._home())
        self._tasks: set[asyncio.Task[Any]] = set()

    # ── config ──

    @property
    def cfg(self) -> dict[str, Any]:
        return autonomy_cfg(self.server.config)

    def gate_applies(self, session: Any) -> bool:
        cfg = self.cfg
        if session.background and not cfg["gate_unattended"]:
            return False  # unattended runs are pre-approved: nobody is there to approve a plan
        override = getattr(session, "scope_override", None)
        if override:
            return True
        return bool(cfg["plan_first"]) and session.perms.mode.value in cfg["gate_modes"]

    def advisor_applies(self, session: Any, key: str) -> bool:
        cfg = self.cfg
        return bool(cfg.get(key)) and session.perms.mode.value in cfg["advisor_modes"]

    def spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for background proposer passes (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ── the gate ──

    async def prepare(self, session: Any, text: str) -> GateResult:
        pre = getattr(session, "preapproved_plan", None)
        if pre:  # /go after /ultraplan: the plan is approved, skip the gate and let fan-out take it
            from k3code.autonomy.ultra import plan_verdict

            session.preapproved_plan = None
            plan = str(pre["plan"])
            return GateResult(prompt=f"{text}\n\nApproved plan (follow it):\n{plan}", verdict=plan_verdict(plan),
                              plan=plan)
        if not self.gate_applies(session):
            return GateResult(prompt=text)
        sid = session.session_id
        override = getattr(session, "scope_override", None)
        session.scope_override = None  # applies to the next task only
        if override:
            verdict = scope.from_override(override, text)
        else:
            verdict = await scope.classify(
                self.server.model_caller, text, Path(session.stored.cwd or "."),
                recent=advisor_mod.transcript_text(session.stored.messages[-6:], per_message=400), session_id=sid,
            )
        h = self.scope_log.verdict(text, verdict, sid)
        session.emit("scope.verdict", {"session_id": sid, "hash": h, **verdict.as_dict()})
        if not verdict.wants_plan:
            return GateResult(prompt=text, verdict=verdict, scope_hash=h)

        captured = await self._planning_turn(session, text, verdict)
        if not captured.approved:
            return GateResult(
                prompt=text, proceed=False, verdict=verdict, scope_hash=h,
                message="Plan not approved; nothing was executed. Rephrase the task or send it again.",
            )
        prompt = f"{text}\n\nApproved plan (follow it):\n{captured.plan}"
        if self.cfg["proposals"]:
            self.spawn(self.propose_from(session, f"Task: {text}\n\nPlan:\n{captured.plan}"))
        return GateResult(prompt=prompt, verdict=verdict, plan=captured.plan, scope_hash=h)

    # ── planning turn ──

    async def _planning_turn(self, session: Any, text: str, verdict: ScopeVerdict) -> _Captured:
        server = self.server
        sid = session.session_id
        router = server.tier_routers().get(Tier.STRONG)
        cap = _Captured()
        plan_id = f"plan-{uuid.uuid4().hex[:8]}"

        def show(status: str, **extra: Any) -> None:
            session.emit(
                "plan.show",
                {"session_id": sid, "plan_id": plan_id, "status": status, "plan": cap.plan,
                 "sections": parse_plan(cap.plan), "scope": verdict.scope, "risk": verdict.risk,
                 "fanout_candidate": verdict.fanout_candidate,
                 "parallelizable": verdict.parallelizable, "subtasks": verdict.suggested_subtasks, **extra},
                importance="essential",
            )

        async def on_plan(plan: str) -> str | None:
            cap.plan = plan
            cap.asked += 1
            show("proposed")
            auto_ok = verdict.risk != "high" or bool(self.cfg.get("auto_do_plans"))
            if session.perms.mode is PermissionMode.AUTO and auto_ok:
                cap.approved, cap.auto, cap.mode = True, True, "auto"
            elif session.perms.mode is PermissionMode.AUTO:
                cap.approved = await server._confirm_plan(session, plan, verdict.risk)
                cap.mode = "auto" if cap.approved else None
            else:
                cap.mode = await server._plan_callback_for(session)(plan)  # existing exit_plan approval flow
                cap.approved = cap.mode is not None
            if cap.approved:
                loop.interrupt()  # the planning loop must not go on to implement
                return cap.mode
            return None

        perms = PermissionState(mode=PermissionMode.PLAN, cwd=session.perms.cwd, add_dirs=list(session.perms.add_dirs))
        loop = AgentLoop(
            router,
            system_prompt=session.system_prompt + PLAN_ADDENDUM,
            max_turns=8,
            headless=False,
            on_event=server._on_router_event,
            cwd=session.perms.cwd,
            approval_callback=await server._approval_callback_for(session),
            plan_callback=on_plan,
            permissions=perms,
            reliability=await server._reliability_for(session),
            session=sid,
            task_kind=TaskKind.PLAN,
        )
        session.current_kind = TaskKind.PLAN.value
        session.emit("status.update", {"kind": "status", "text": "planning", "state": "working"})
        prompt = f"Task:\n{text}\n\nScope: {verdict.scope}. Risk: {verdict.risk}. {verdict.reason}"
        if verdict.suggested_subtasks:
            mark = " (parallelizable)" if verdict.parallelizable else ""
            prompt += f"\nSuggested subtasks{mark}: " + "; ".join(verdict.suggested_subtasks)
        last_text = ""
        async for event in loop.run(prompt, max_tokens=server.config.max_tokens, history=session.history):
            server._on_stream_event(session, event)
            if event.type == "done" and event.message and event.message.role == "assistant" and event.message.content:
                last_text = event.message.content
        if not cap.plan and last_text.strip() and not loop.interrupted:
            await on_plan(last_text.strip())  # the model answered with a plan but skipped exit_plan
        if not cap.approved:
            if cap.plan:
                show("rejected")
            return cap

        extra: dict[str, Any] = {"auto_approved": cap.auto}
        if self.advisor_applies(session, "advisor_on_plan"):
            try:
                extra["advisor"] = await advisor_mod.advise(
                    server.model_caller, f"Task: {text}\n\nPlan:\n{cap.plan}", "Critique this approved plan.",
                    session_id=sid, brief=True, timeout=60,
                )
            except Exception:  # noqa: BLE001 - a missing critique must not stop the task
                logger.warning("advisor critique after plan failed", exc_info=True)
        show("approved", **extra)
        return cap

    # ── proposer ──

    async def propose_from(self, session: Any, context: str) -> None:
        if not self.cfg["proposals"]:
            return
        hub = getattr(self.server, "learning", None)
        project = learning_project(session)
        kw: dict[str, Any] = {}
        if hub is not None and hub.enabled:
            kw = {"ranker": hub.ranker(project), "preferences": hub.preferences(), "project": project}
        for p in await propose(self.server.model_caller, self.proposals, context, session_id=session.session_id, **kw):
            self.emit_proposal(session, p)

    def emit_proposal(self, session: Any, p: Any) -> None:
        session.emit("proposal.show", {"session_id": session.session_id, "id": p.id, "kind": p.kind,
                                       "text": p.text, "action": p.action, "key": p.key})

    # ── after the task ──

    def finish(self, session: Any, result: GateResult, status: str, final_text: str, text: str) -> None:
        """Record the outcome and (when it went well) spawn the post-task proposer."""
        if result.scope_hash:
            self.scope_log.outcome(result.scope_hash, status,
                                   planned=bool(result.plan), fanout_candidate=bool(
                                       result.verdict and result.verdict.fanout_candidate))
        if status == "done" and self.cfg["proposals"] and self.gate_applies(session) and final_text:
            self.spawn(self.propose_from(session, f"Task: {text}\n\nResult:\n{final_text[-3000:]}"))
