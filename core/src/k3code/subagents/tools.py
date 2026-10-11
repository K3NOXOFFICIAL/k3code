"""``task`` and ``task_result``: let the model delegate work to child sessions."""

from __future__ import annotations

from typing import Any

from k3code.autonomy.agents import session_limits
from k3code.providers.types import ToolSpec
from k3code.subagents.runner import DepthLimit, SpawnRefused

#: the tools of an agent type that cannot change anything another child could observe
READ_ONLY_TOOLS = frozenset({"read", "grep", "glob"})


def register_task_tools(
    reg: Any, server: Any, parent: Any, *, depth: int = 1, parent_child_id: str | None = None
) -> None:
    """Register ``task``/``task_result`` on ``reg``. ``depth`` is the depth a child spawned here would have."""
    mgr = server.subagents

    async def tool_task(arguments: dict[str, Any], *, cwd: Any = None) -> dict[str, Any]:
        description = str(arguments.get("description") or "").strip()
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return {"error": "task needs a prompt"}
        try:
            mgr.admit(parent, parent_child_id, session_limits(server.config, parent))
            h = mgr.spawn(
                parent,
                description=description or prompt[:60],
                prompt=prompt,
                agent_type=arguments.get("agent_type") or None,
                tier=arguments.get("tier") or None,
                isolation=str(arguments.get("isolation") or "none"),
                depth=depth,
                parent_child_id=parent_child_id,
            )
        except (DepthLimit, SpawnRefused, ValueError) as e:
            return {"error": str(e)}
        mgr.note_spawn(parent.session_id, parent_child_id)
        if arguments.get("background"):
            return {"content": (f'Started sub-agent {h.id} in the background. Poll with task_result(id="{h.id}").')}
        await mgr.wait(h)
        return {"content": h.render()}

    def parallel_when(arguments: Any) -> bool:
        """Several ``task`` calls of one reply may run at the same time when each child can only read: an explorer or
        planner (tools within read/grep/glob) in the shared checkout. A child that can write, runs a command or works
        in a worktree stays a barrier. Anything unexpected (a list where a name belongs, an unknown type) is serial:
        the call then gets its normal invalid-arguments answer."""
        try:
            if not isinstance(arguments, dict) or str(arguments.get("isolation") or "none") != "none":
                return False
            name = arguments.get("agent_type")
            if name is not None and not isinstance(name, str):
                return False
            atype = mgr.resolve_type(name or None, parent.perms.cwd)
            return bool(atype.tools) and set(atype.tools) <= READ_ONLY_TOOLS
        except Exception:  # noqa: BLE001 - this only decides whether a call may overlap; it must never raise
            return False

    def parallel_limit() -> int:
        """How many task calls may start together: what the session's parallel limit still allows. A longer run of
        read-only calls is cut into chunks of this size, so none is refused that running them in turn would have run."""
        try:
            limits = session_limits(server.config, parent)
            return max(1, limits.max_parallel - mgr.live(parent.session_id, parent_child_id))
        except Exception:  # noqa: BLE001
            return 1

    tool_task.parallel_when = parallel_when  # type: ignore[attr-defined]  # read by AgentLoop._parallel_ok
    tool_task.parallel_limit = parallel_limit  # type: ignore[attr-defined]  # read by AgentLoop._batches

    async def tool_task_result(arguments: dict[str, Any], *, cwd: Any = None) -> dict[str, Any]:
        h = mgr.handles.get(str(arguments.get("id") or ""))
        if h is None or h.parent_sid != parent.session_id:
            return {"error": f"no such sub-agent: {arguments.get('id')}"}
        if arguments.get("wait") and not h.done:
            await mgr.wait(h)
        if not h.done:
            return {"content": f"Sub-agent {h.id} is still {h.status} ({h.tool_count} tool calls so far)."}
        return {"content": h.render()}

    reg.register(
        ToolSpec(
            name="task",
            description=(
                "Delegate a self-contained job to a sub-agent; returns its final answer. Use for parallel "
                "investigation, isolated work (isolation=worktree) or review. agent_type: explorer (read-only), "
                "worker, reviewer, planner or a custom name. background=true returns a handle; poll it with "
                "task_result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "3-8 word label"},
                    "prompt": {"type": "string", "description": "Everything the sub-agent needs; no other context"},
                    "agent_type": {"type": "string"},
                    "tier": {"type": "string", "enum": ["main", "strong", "cheap", "fast"]},
                    "isolation": {"type": "string", "enum": ["none", "worktree"]},
                    "background": {"type": "boolean"},
                },
                "required": ["description", "prompt"],
            },
            side_effect=True,
        ),
        tool_task,
    )
    reg.register(
        ToolSpec(
            name="task_result",
            description="Result (or progress) of a background sub-agent started with task(background=true).",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}, "wait": {"type": "boolean"}},
                "required": ["id"],
            },
            side_effect=False,
        ),
        tool_task_result,
    )
