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


#: The one line on the recall tool: static, so the prompt (and the provider cache of it) never moves with the archive.
RECALL_NOTE = (
    "All earlier sessions (any age, any project) and learned notes are searchable with `recall`; when the user refers"
    " to something from before, or assumes context you lack, search before answering."
)


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
    recall: bool = False,
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
    from k3code.autonomy import advisor as _advisor
    from k3code.autonomy import autonomy_cfg

    if _advisor.session_advisor(session_meta, autonomy_cfg(config).get("advisor", "")):  # the `advisor` tool exists
        parts.append(_advisor.PROMPT_NOTE)
    if recall:  # the caller registers the `recall` tool (the gateway's sessions); the same words every time
        parts.append(RECALL_NOTE)
    ctx = getattr(config, "context", None) or {}  # M1: prompt-size knobs the optimizer may tune (defaults unchanged)

    # What background writers change (a re-scan of the stack, the daily distill of USER.md, an accepted gotcha, the
    # optimizer, a skill file that appears) is taken once per session when ``frozen`` is given: from any other session,
    # one of them rewrote this session's prompt and with it the provider's cache of the whole conversation.
    def once(key: str, make: Any, *, empty_ok: bool = True) -> str:
        if frozen is None:
            return make()
        if key not in frozen:
            value = make()
            if value or empty_ok:
                frozen[key] = value
            return value
        return frozen[key]

    # ``skills=False``: the caller has no skill tool, so an index of skills it cannot call would only cost tokens. A
    # skill added mid-session is found by the ``skill`` tool at once and listed here from the next compaction; an index
    # with no skills is not kept, or a session that started without any would never list the first
    limit = int(ctx.get("skill_prompt_limit", PROMPT_LIMIT))
    if skills and (
        index := once("skills", lambda: skills_prompt(cwd, list(config.skills.roots), limit=limit), empty_ok=False)
    ):
        parts.append(index)
    # a server that connects after the first turn adds its names to the index: kept out until the prompt is refreshed
    # (``mcp_tool_search`` finds its tools meanwhile, and a restarting server keeps its tools: McpManager._last_tools).
    # Nothing yet is only kept once the servers have been started: a size estimate before the first turn asks too
    index = once("mcp", lambda: mcp_prompt(mcp), empty_ok=not getattr(mcp, "cold", False)) if mcp is not None else ""
    if index:
        parts.append(index)
    from k3code.learning.gotchas import gotchas_prompt, user_gotchas_prompt
    from k3code.learning.optimizer import overlay_prompt
    from k3code.learning.projectprep import facts_prompt

    # no facts yet is not frozen: the scan of a new project stores its result a moment after the session starts, and an
    # empty value kept for the whole session left it without them (nothing a background writer rewrites mid-session)
    if facts := once("facts", lambda: facts_prompt(cwd), empty_ok=False):  # the stored stack scan
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
