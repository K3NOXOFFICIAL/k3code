"""Sub-agent runtime: child agent loops spawned by the ``task`` tool, fan-out and the ultra commands.

A child is an :class:`AgentLoop` with its own reliability bundle (the retry wrapper is bound to one router,
so parallel children must not share the parent's), the parent's permission mode and add-dirs, the
``subagent`` task kind and a tier chosen by argument > agent type > policy. Events go to the parent
session as ``subagent.*`` (the TUI strip and /agents overlay already render them).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code import userhooks
from k3code.agent.loop import AgentLoop
from k3code.autonomy import autonomy_cfg
from k3code.permissions import PermissionMode
from k3code.permissions.state import PermissionState
from k3code.prompting import build_system_prompt
from k3code.providers.types import ToolCall
from k3code.reliability.hooks import Reliability, ReliabilityFlags, ReliabilitySettings
from k3code.routing.tiers import TaskKind, Tier, tier_for
from k3code.subagents import worktree as wt_mod
from k3code.subagents.types import AgentType, load_agent_types
from k3code.tools import jobs as tool_jobs

logger = logging.getLogger(__name__)

MAX_DEPTH = 2
#: finished handles kept for /agents and the strip; older ones are evicted (the registry grew for the daemon's life)
KEEP_FINISHED_HANDLES = 200


class DepthLimit(Exception):
    pass


@dataclass
class Handle:
    id: str
    description: str
    agent_type: str
    tier: str
    depth: int  # 1 = spawned by a session, 2 = spawned by a child
    parent_sid: str
    parent_child_id: str | None = None
    isolation: str = "none"
    status: str = "queued"  # queued | running | completed | failed | interrupted
    result: str = ""
    error: str = ""
    tool_count: int = 0
    last_tool: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: float = 0.0  # time.monotonic(): durations only
    finished_at: float = 0.0
    #: wall clock (Unix seconds) at start: what clients get as ``started_at``; the TUI turned the monotonic value
    #: (seconds since boot) into a date in 1970 and showed every sub-agent as running for ~56 years
    started_wall: float = 0.0
    #: messages the user typed for this child in the agents overlay (`e`); its loop takes them before the next call
    steer_queue: list[str] = field(default_factory=list)
    model: str = ""
    cwd: str = ""
    worktree: wt_mod.Worktree | None = None
    diff: str = ""
    diff_stat: str = ""
    branch: str = ""
    #: none | merged | conflict | no_changes | pending
    merge: str = "none"
    merge_output: str = ""
    task: asyncio.Task[Any] | None = field(default=None, repr=False)
    loop: AgentLoop | None = field(default=None, repr=False)
    tail: list[str] = field(default_factory=list)
    index: int = 0
    count: int = 1

    @property
    def done(self) -> bool:
        return self.status in ("completed", "failed", "interrupted")

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "agent_type": self.agent_type,
            "tier": self.tier,
            "description": self.description,
            "result": self.result,
            "error": self.error,
            "tool_count": self.tool_count,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "branch": self.branch,
            "merge": self.merge,
            "diff_stat": self.diff_stat,
        }

    def render(self) -> str:
        """What the parent model sees as the tool result."""
        if self.status == "failed":
            return f"Sub-agent {self.id} failed: {self.error}"
        if self.status == "interrupted":
            return f"Sub-agent {self.id} was interrupted. Partial result:\n{self.result}"
        out = [self.result.strip() or "(the sub-agent returned no text)"]
        if self.isolation == "worktree":
            if self.merge == "no_changes":
                out.append("\n[worktree] no file changes.")
            elif self.merge == "merged":
                out.append(
                    f"\n[worktree] changes merged into your checkout from branch {self.branch}.\n{self.diff_stat}"
                )
            elif self.merge == "conflict":
                out.append(
                    f"\n[worktree] MERGE CONFLICT: not merged. The work is on branch {self.branch} "
                    f"(worktree .k3code/worktrees/{self.id}); merge it yourself.\n{self.diff_stat}\n{self.merge_output}"
                )
            elif self.merge == "pending":
                out.append(f"\n[worktree] changes left on branch {self.branch} (not merged).\n{self.diff_stat}")
            if self.diff and self.merge != "merged":
                out.append("\n```diff\n" + self.diff[:12000] + "\n```")
        return "".join(out)


def child_reliability(config: Any, child_id: str, home: Path) -> Reliability:
    """Reliability bundle for a child: like the session's, but no netwatch probe per child."""
    raw = dict(getattr(config, "reliability", None) or {})
    if raw.get("enabled") is False:
        return Reliability.disabled()
    flags_raw = dict(raw.get("flags") or {})
    flags_raw["netwatch"] = False
    settings = ReliabilitySettings(flags=ReliabilityFlags(**flags_raw))
    # The budget caps too: a child's bundle used to be built without them, so sub-agents (the biggest spenders:
    # ultracode fans out dozens) ran with no budget guard at all. The day budget is the process-wide ledger.
    for key in ("max_wait", "max_park_seconds", "session_tokens", "session_usd", "day_tokens", "day_usd"):
        if key in raw:
            setattr(settings, key, raw[key])
    return Reliability.from_settings(settings, session=child_id, home=home)


class SubagentManager:
    """Registry and runner of child agents, bound to a GatewayServer."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.handles: dict[str, Handle] = {}
        #: the agents overlay's `p` / `/agents pause`: no new child starts while set; running ones finish
        self.paused = False
        self._types: dict[str, AgentType] | None = None
        self._types_key: str = ""

    # ── agent types ──

    def agent_types(self, cwd: str | Path) -> dict[str, AgentType]:
        key = str(cwd)
        if self._types is None or self._types_key != key:
            self._types = load_agent_types(cwd, self.server._home())
            self._types_key = key
        return self._types

    def resolve_type(self, name: str | None, cwd: str | Path) -> AgentType:
        types = self.agent_types(cwd)
        name = name or "worker"
        if name not in types:
            raise ValueError(f"unknown agent_type {name!r}; available: {', '.join(sorted(types))}")
        return types[name]

    # ── queries ──

    def for_session(self, sid: str) -> list[Handle]:
        return [h for h in self.handles.values() if h.parent_sid == sid]

    def used_agents(self, sid: str) -> int:
        return len(self.for_session(sid))

    def tokens_for(self, handles: list[Handle]) -> int:
        return sum(h.tokens_in + h.tokens_out for h in handles)

    # ── spawning ──

    def spawn(
        self,
        parent: Any,
        *,
        description: str,
        prompt: str,
        agent_type: str | None = None,
        tier: str | None = None,
        isolation: str = "none",
        depth: int = 1,
        parent_child_id: str | None = None,
        auto_merge: bool = True,
        index: int = 0,
        count: int = 1,
        worktree: wt_mod.Worktree | None = None,
    ) -> Handle:
        """Create the child and start it as a task; the caller awaits :meth:`wait` or polls."""
        if getattr(self.server, "halted", False):  # /daemon pause: no new child runs anywhere
            raise RuntimeError("daemon is halted (/daemon pause): sub-agents are not started")
        if self.paused:
            raise RuntimeError("spawning is paused (/agents resume): sub-agents are not started")
        if depth > MAX_DEPTH:
            raise DepthLimit(f"sub-agent depth limit ({MAX_DEPTH}) reached")
        if isolation not in ("none", "worktree"):
            raise ValueError("isolation must be 'none' or 'worktree'")
        atype = self.resolve_type(agent_type, parent.perms.cwd)
        chosen = tier or atype.tier or tier_for(TaskKind.SUBAGENT, self.server.config.task_tiers).value
        try:
            Tier(chosen)
        except ValueError as e:
            raise ValueError(f"unknown tier {chosen!r}") from e
        h = Handle(
            id=f"sa-{uuid.uuid4().hex[:8]}",
            description=description,
            agent_type=atype.name,
            tier=chosen,
            depth=depth,
            parent_sid=parent.session_id,
            parent_child_id=parent_child_id,
            isolation=isolation,
            index=index,
            count=count,
        )
        if worktree is not None:  # a rework round continues in the same worktree
            h.worktree, h.isolation, h.branch = worktree, "worktree", worktree.branch
        self.handles[h.id] = h
        self._emit(parent, "subagent.spawn_requested", h)
        h.task = asyncio.get_running_loop().create_task(self._run(parent, h, atype, prompt, auto_merge))
        return h

    async def wait(self, h: Handle) -> Handle:
        """Wait for the child. A cancellation of the *child* (interrupt) is its result; a cancellation of the
        *waiter* (/stop on the parent turn) is not swallowed, and takes the child down with it. The old
        ``suppress(CancelledError)`` ate both, so /stop was ignored by every ultracode/ultraplan/fan-out chain."""
        task = h.task  # a finished child drops its task ref (see _run)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled() or task.done():
                    return h  # the child itself was interrupted
                task.cancel()  # we were cancelled while the child still runs: stop it too
                raise
        return h

    async def run(self, parent: Any, **kw: Any) -> Handle:
        return await self.wait(self.spawn(parent, **kw))

    def interrupt(self, hid: str) -> bool:
        h = self.handles.get(hid)
        if h is None or h.done:
            return False
        for child in list(self.handles.values()):  # a child's own sub-agents go down with it
            if child.parent_child_id == h.id and not child.done:
                self.interrupt(child.id)
        if h.loop is not None:
            h.loop.interrupt()
        if h.task is not None:
            h.task.cancel()
        return True

    def interrupt_session(self, sid: str) -> None:
        for h in self.for_session(sid):
            self.interrupt(h.id)

    # ── events ──

    def _payload(self, h: Handle, **extra: Any) -> dict[str, Any]:
        return {
            "subagent_id": h.id,
            "parent_id": h.parent_child_id,
            "depth": h.depth - 1,
            "goal": h.description,
            "model": h.model or h.tier,
            "task_index": h.index,
            "task_count": h.count,
            "tool_count": h.tool_count,
            "status": h.status,
            "agent_type": h.agent_type,
            "tier": h.tier,
            "session_id": h.parent_sid,
            "input_tokens": h.tokens_in,
            "output_tokens": h.tokens_out,
            **extra,
        }

    def _emit(self, parent: Any, kind: str, h: Handle, **extra: Any) -> None:
        parent.emit(kind, self._payload(h, **extra))
        if kind in ("subagent.start", "subagent.complete"):
            self.server.broadcast_active_list()  # children share the strip with /bg and unattended sessions

    # ── execution ──

    async def _run(self, parent: Any, h: Handle, atype: AgentType, prompt: str, auto_merge: bool) -> None:
        from k3code.gateway.server import _ctx_session  # late: gateway imports this package

        _ctx_session.set(parent)  # router/reliability events reach the parent's clients
        h.started_at = time.monotonic()
        h.started_wall = time.time()
        h.status = "running"
        cwd = Path(parent.perms.cwd)
        try:
            if h.isolation == "worktree":
                if h.worktree is None:
                    h.worktree = await wt_mod.create(cwd, h.id)
                if h.worktree is not None:
                    cwd = h.worktree.path
                    h.branch = h.worktree.branch
                else:
                    h.isolation = "none"  # not a git repo: the child shares the parent's cwd
            h.cwd = str(cwd)
            self._emit(parent, "subagent.start", h)
            await self._drive(parent, h, atype, prompt, cwd)
            if h.status == "running":
                h.status = "completed"
            if h.worktree is not None:
                await self._finish_worktree(h, auto_merge)
        except asyncio.CancelledError:
            h.status = "interrupted"
            if h.worktree is not None:
                with contextlib.suppress(Exception):
                    await self._finish_worktree(h, False)
        except Exception as e:  # noqa: BLE001 - a child failing must come back as a result, not crash the parent
            logger.exception("sub-agent %s failed", h.id)
            h.status = "failed"
            h.error = str(e)
            if h.worktree is not None:  # the checkout and branch of a dead child used to stay forever
                with contextlib.suppress(Exception):
                    await self._finish_worktree(h, False)
        finally:
            h.finished_at = time.monotonic()
            dur = h.finished_at - h.started_at
            self._emit(
                parent,
                "subagent.complete",
                h,
                duration_seconds=dur,
                summary=(h.result or h.error)[:2000],
                text=(h.result or h.error)[:2000],
            )
            self._record_usage(h)
            rel = getattr(parent, "reliability", None)
            if rel is not None and rel.governor is not None:  # the parent's session budget includes its children
                rel.governor.charge(h.tokens_in, h.tokens_out)
            h.loop = None  # the loop holds the child's whole history; the handle keeps only what /agents shows
            h.task = None
            self._prune()

    def _prune(self) -> None:
        """Evict the oldest finished handles beyond ``KEEP_FINISHED_HANDLES``."""
        finished = sorted((x for x in self.handles.values() if x.done), key=lambda x: x.finished_at)
        for old in finished[: max(0, len(finished) - KEEP_FINISHED_HANDLES)]:
            self.handles.pop(old.id, None)

    async def _finish_worktree(self, h: Handle, auto_merge: bool) -> None:
        wt = h.worktree
        assert wt is not None
        committed = await wt_mod.commit_all(wt, f"k3code sub-agent {h.id}: {h.description[:60]}")
        h.diff = await wt_mod.diff(wt)
        h.diff_stat = await wt_mod.diff_stat(wt)
        if not committed and not h.diff:
            h.merge = "no_changes"
            await wt_mod.remove(wt, keep_branch=False)
            return
        if not auto_merge:
            h.merge = "pending"
            if h.status in ("failed", "interrupted"):
                # nobody continues in this checkout: free the directory, the work stays on the branch
                await wt_mod.remove(wt, keep_branch=True)
            return
        ok, out = await wt_mod.merge(wt)
        h.merge_output = out
        h.merge = "merged" if ok else "conflict"
        await wt_mod.remove(wt, keep_branch=not ok)

    def _record_usage(self, h: Handle) -> None:
        try:
            self.server.usage.record(
                "subagent",
                session=h.parent_sid,
                model=h.model,
                tokens_in=h.tokens_in,
                tokens_out=h.tokens_out,
                seconds=h.finished_at - h.started_at,
                tier=h.tier,
                task_kind=TaskKind.SUBAGENT.value,
                detail=f"{h.agent_type}: {h.description[:80]}",
            )
        except Exception:  # noqa: BLE001
            logger.debug("sub-agent usage row failed", exc_info=True)

    def build_loop(self, parent: Any, h: Handle, atype: AgentType, cwd: Path, reliability: Reliability) -> AgentLoop:
        server = self.server
        perms = PermissionState(mode=PermissionMode(parent.perms.mode), cwd=cwd, add_dirs=list(parent.perms.add_dirs))
        base = build_system_prompt(
            parent.system_prompt,
            cwd=cwd,
            config=server.config,
            session_meta=parent.stored.meta,
            mcp=server.mcp,
        )
        note = ""
        if h.isolation == "worktree":
            note = (
                f"\n\nYou work in an isolated git worktree ({cwd}) on branch {h.branch}. Edit freely there; "
                "your changes are committed and merged back by the harness. Do not run git commands that "
                "change branches."
            )
        system = f"[agent:{atype.name}] {h.description}\n\n{base}\n\n{atype.prompt}{note}"
        router = server.tier_routers().get(Tier(h.tier))
        loop = AgentLoop(
            router,
            system_prompt=system,
            max_turns=server.config.max_turns,
            headless=False,
            on_event=server._on_router_event,
            cwd=cwd,
            permissions=perms,
            reliability=reliability,
            session=h.id,
            background=parent.background,
            unattended=True,
            unattended_network=bool(autonomy_cfg(server.config)["unattended_network"]),
            task_kind=TaskKind.SUBAGENT.value,
            approval_callback=getattr(parent, "_approval_cb", None),
        )
        from k3code.extratools import register_skill_tool

        # trust is keyed on the project the user trusted (a worktree child's cwd is a fresh path under it);
        # the hook commands run where the child works
        loop.hooks = userhooks.load(parent.perms.cwd, h.id)
        loop.hooks.cwd = cwd

        if not atype.tools or "skill" in atype.tools:
            register_skill_tool(loop.tools, cwd, list(server.config.skills.roots))
        if h.depth < MAX_DEPTH and (not atype.tools or "task" in atype.tools):
            from k3code.subagents.tools import register_task_tools

            register_task_tools(loop.tools, server, parent, depth=h.depth + 1, parent_child_id=h.id)
        if atype.tools:
            allowed = set(atype.tools) | ({"task_result"} if "task" in atype.tools else set())
            for name in list(loop.tools.names()):
                if name not in allowed and name != "exit_plan":
                    loop.tools._tools.pop(name, None)  # noqa: SLF001

        def take_steer() -> list[str]:
            taken = h.steer_queue[:]
            h.steer_queue.clear()
            return taken

        loop.take_steer = take_steer
        return loop

    async def _drive(self, parent: Any, h: Handle, atype: AgentType, prompt: str, cwd: Path) -> None:
        server = self.server
        server._ensure_router(parent.stored.model or None)
        reliability = child_reliability(server.config, h.id, server._home())
        parent_approval = await server._approval_callback_for(parent)
        parent._approval_cb = parent_approval  # noqa: SLF001 - shared with every child of this session
        loop = self.build_loop(parent, h, atype, cwd, reliability)
        h.loop = loop
        final = ""
        try:
            stream = loop.run(prompt, max_tokens=server.config.max_tokens, temperature=server.config.temperature)
            seen: set[str] = set()  # call ids already counted (claude_cli streams them, then repeats them on done)

            def on_tool(tc: ToolCall) -> None:
                if tc.id in seen:
                    return
                seen.add(tc.id)
                h.tool_count += 1
                h.last_tool = tc.name
                args = tc.arguments or {}
                preview = str(
                    args.get("command") or args.get("path") or args.get("pattern") or args.get("description") or ""
                )[:120]
                h.tail.append(f"{tc.name} {preview}".strip())
                self._emit(parent, "subagent.tool", h, tool_name=tc.name, tool_preview=preview, text=preview)

            async for ev in stream:
                if ev.type == "tool_call" and ev.tool_call:
                    on_tool(ev.tool_call)
                elif ev.type == "done" and ev.message and ev.message.role == "assistant":
                    msg = ev.message
                    # openai_compat and anthropic stream no tool_call events: their calls arrive on this message
                    for tc in msg.tool_calls:
                        on_tool(tc)
                    seen.clear()
                    if msg.usage:
                        h.tokens_in += msg.usage.prompt_tokens
                        h.tokens_out += msg.usage.completion_tokens
                    if msg.content:
                        final = msg.content
                        if msg.tool_calls:
                            self._emit(parent, "subagent.progress", h, text=msg.content.strip()[:200])
                    h.model = server.last_attempt[1] or h.model
        finally:
            h.result = final.strip()
            # the child's background bash jobs are filed under its own id: no session close ever reaps them
            with contextlib.suppress(Exception):
                await tool_jobs.reap(h.id)
            await self._stop_reliability(reliability)
        if loop.interrupted:
            h.status = "interrupted"

    @staticmethod
    async def _stop_reliability(rel: Reliability) -> None:
        with contextlib.suppress(Exception):
            await rel.stop()
