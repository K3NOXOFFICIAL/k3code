"""System prompt composer: base prompt + output style + memory + skills index + deferred MCP names."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code import outputstyle
from k3code.extratools import mcp_prompt
from k3code.memory import MAX_MEMORY_CHARS, memory_prompt
from k3code.skills import PROMPT_LIMIT, skills_prompt

_BASE_PATH = Path(__file__).parent / "prompts" / "system.md"


def base_prompt() -> str:
    if _BASE_PATH.is_file():
        return _BASE_PATH.read_text(encoding="utf-8")
    return "You are a helpful coding assistant."


def effective_style(config: Any, session_meta: dict[str, Any] | None) -> str:
    return str((session_meta or {}).get("output_style") or getattr(config, "output_style", "") or "default")


def build_system_prompt(
    base: str,
    *,
    cwd: str | Path,
    config: Any,
    session_meta: dict[str, Any] | None = None,
    mcp: Any = None,
) -> str:
    # The prompt is sent as one prefix that provider caches match from the top, so the sections that change rarely
    # come first and the learned ones (USER.md's auto section is rewritten by the distiller) come last: a rewrite
    # then invalidates only its own tail. Nothing here may depend on the time, a counter or the turn.
    parts = [base.rstrip()]
    style = outputstyle.style_text(effective_style(config, session_meta), cwd)
    if style:
        parts.append(style)
    ctx = getattr(config, "context", None) or {}  # M1: prompt-size knobs the optimizer may tune (defaults unchanged)
    if skills := skills_prompt(cwd, list(config.skills.roots), limit=int(ctx.get("skill_prompt_limit", PROMPT_LIMIT))):
        parts.append(skills)
    if mcp is not None and (m := mcp_prompt(mcp)):
        parts.append(m)
    from k3code.learning.projectprep import facts_prompt

    if facts := facts_prompt(cwd):  # the stored stack scan; with or without a memory file, changes on re-scan only
        parts.append(facts)
    if mem := memory_prompt(cwd, limit=int(ctx.get("memory_chars", MAX_MEMORY_CHARS))):
        parts.append(mem)
    from k3code.learning.gotchas import gotchas_prompt, user_gotchas_prompt
    from k3code.learning.optimizer import overlay_prompt

    if gotchas := gotchas_prompt(cwd):  # accepted project gotchas; changes only when one is accepted
        parts.append(gotchas)
    if machine := user_gotchas_prompt():  # lines learned alike in several projects; changes only when one is added
        parts.append(machine)

    if overlay := overlay_prompt():
        parts.append(overlay)
    return "\n\n".join(parts) + "\n"
