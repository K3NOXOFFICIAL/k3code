"""System prompt composer: base prompt + output style + memory + skills index + deferred MCP names."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code import outputstyle
from k3code.extratools import mcp_prompt
from k3code.memory import memory_prompt
from k3code.skills import skills_prompt

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
    parts = [base.rstrip()]
    style = outputstyle.style_text(effective_style(config, session_meta), cwd)
    if style:
        parts.append(style)
    if mem := memory_prompt(cwd):
        parts.append(mem)
    if skills := skills_prompt(cwd, list(config.skills.roots)):
        parts.append(skills)
    if mcp is not None and (m := mcp_prompt(mcp)):
        parts.append(m)
    from k3code.learning.optimizer import overlay_prompt

    if overlay := overlay_prompt():
        parts.append(overlay)
    return "\n\n".join(parts) + "\n"
