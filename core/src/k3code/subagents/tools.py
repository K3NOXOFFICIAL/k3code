"""``task`` and ``task_result``: let the model delegate work to child sessions."""

from __future__ import annotations

from typing import Any

from k3code.providers.types import ToolSpec
from k3code.subagents.runner import DepthLimit


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
            h = mgr.spawn(
                parent, description=description or prompt[:60], prompt=prompt,
                agent_type=arguments.get("agent_type") or None, tier=arguments.get("tier") or None,
                isolation=str(arguments.get("isolation") or "none"), depth=depth, parent_child_id=parent_child_id,
            )
        except DepthLimit as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}
        if arguments.get("background"):
            return {"content": (
                f"Started sub-agent {h.id} in the background. Poll with task_result(id=\"{h.id}\").")}
        await mgr.wait(h)
        return {"content": h.render()}

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
