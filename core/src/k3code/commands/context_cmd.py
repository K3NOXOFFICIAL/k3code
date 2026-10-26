"""/context: what fills the session's context window, and how far it is from compaction."""

from __future__ import annotations

import json
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply
from k3code.context_budget import compact_threshold, context_window
from k3code.prompting import build_system_prompt, tokens_of
from k3code.tools import build_registry as build_tool_registry


def context_rows(system_prompt: str, tool_schemas: str, conversation_tokens: int) -> list[tuple[str, int]]:
    """``(part, estimated tokens)`` of what every request carries: the system prompt, the tool schemas, the
    conversation so far."""
    return [
        ("system prompt", tokens_of(system_prompt)),
        ("tool schemas", tokens_of(tool_schemas)),
        ("conversation", conversation_tokens),
    ]


def render_context(rows: list[tuple[str, int]], window: int, compact_at: int) -> str:
    """The table: tokens and share of the window per part, then the total against the window and the compaction mark."""
    total = sum(n for _, n in rows)
    width = max(len(name) for name, _ in [*rows, ("total", 0)])
    lines = [f"{'part':<{width}}  {'tokens':>8}  {'of window':>9}"]
    lines += [f"{name:<{width}}  {n:>8,}  {n / window:>8.1%}" for name, n in rows]
    lines.append(f"{'total':<{width}}  {total:>8,}  {total / window:>8.1%}")
    lines.append("")
    lines.append(
        f"window {window:,} tokens; the session compacts itself at {compact_at:,} ({compact_at / window:.0%}); "
        f"free {max(0, window - total):,}. Estimates (about 4 characters per token); /compact [focus] folds it now."
    )
    return "\n".join(lines)


class ContextCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="context", help="Show what fills the context window and how close compaction is")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        from k3code.gateway.server import _estimate_tokens  # the gateway imports the commands: not at module level

        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return reply("No active session.")
        model_id = live.stored.model or ctx.config.default_model
        prompt = build_system_prompt(
            live.system_prompt,
            cwd=live.perms.cwd,
            config=ctx.config,
            session_meta=live.stored.meta,
            mcp=ctx.mcp,
            session_id=live.session_id,
        )
        schemas = json.dumps(
            [
                {"name": s.name, "description": s.description, "parameters": s.parameters}
                for s in build_tool_registry().specs()
            ],
            ensure_ascii=False,
        )
        conversation = [m for m in live.stored.messages if m.get("role") != "system"]
        rows = context_rows(prompt, schemas, _estimate_tokens(conversation))
        return reply(
            render_context(rows, context_window(ctx.config, model_id), compact_threshold(ctx.config, model_id))
        )
