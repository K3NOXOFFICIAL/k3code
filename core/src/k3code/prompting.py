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
    skills: bool = True,
    learned: list[str] | None = None,
    frozen: dict[str, Any] | None = None,
) -> str:
    # The prompt is sent as one prefix that provider caches match from the top, so the sections that change rarely
    # come first and the learned ones last. Every message follows the prompt, so any change to it makes the provider
    # re-write the whole conversation, not only its own tail: a session therefore passes ``learned`` (a snapshot of
    # the notes the background review writes, taken when the session's prompt is first built) and refreshes it where
    # the history is rewritten anyway. Nothing here may depend on the time, a counter or the turn.
    parts = [base.rstrip()]
    style = outputstyle.style_text(effective_style(config, session_meta), cwd)
    if style:
        parts.append(style)
    ctx = getattr(config, "context", None) or {}  # M1: prompt-size knobs the optimizer may tune (defaults unchanged)
    # ``skills=False``: the caller has no skill tool, so an index of skills it cannot call would only cost tokens
    if skills and (
        index := skills_prompt(cwd, list(config.skills.roots), limit=int(ctx.get("skill_prompt_limit", PROMPT_LIMIT)))
    ):
        parts.append(index)
    if mcp is not None and (m := mcp_prompt(mcp)):
        parts.append(m)
    from k3code.learning.gotchas import gotchas_prompt, user_gotchas_prompt
    from k3code.learning.optimizer import overlay_prompt
    from k3code.learning.projectprep import facts_prompt

    # What background writers change (a re-scan of the stack, the daily distill of USER.md, an accepted gotcha, the
    # optimizer) is taken once per session when ``frozen`` is given: from any other session, one of them rewrote this
    # session's prompt and with it the provider's cache of the whole conversation.
    def once(key: str, make: Any) -> str:
        if frozen is None:
            return make()
        if key not in frozen:
            frozen[key] = make()
        return frozen[key]

    if facts := once("facts", lambda: facts_prompt(cwd)):  # the stored stack scan
        parts.append(facts)
    mem_limit = int(ctx.get("memory_chars", MAX_MEMORY_CHARS))
    if mem := memory_prompt(cwd, limit=mem_limit, learned=learned, frozen=frozen):
        parts.append(mem)
    if gotchas := once("gotchas", lambda: gotchas_prompt(cwd)):  # accepted project gotchas
        parts.append(gotchas)
    if machine := once("machine_gotchas", user_gotchas_prompt):  # lines learned alike in several projects
        parts.append(machine)
    if overlay := once("overlay", overlay_prompt):
        parts.append(overlay)
    return "\n\n".join(parts) + "\n"
