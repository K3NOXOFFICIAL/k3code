"""System prompt composer: base prompt + output style + memory + skills index + deferred MCP names."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from k3code import outputstyle
from k3code.extratools import mcp_prompt
from k3code.memory import memory_limit, memory_prompt
from k3code.skills import PROMPT_LIMIT, skills_prompt

_BASE_PATH = Path(__file__).parent / "prompts" / "system.md"


def base_prompt() -> str:
    if _BASE_PATH.is_file():
        return _BASE_PATH.read_text(encoding="utf-8")
    return "You are a helpful coding assistant."


def effective_style(config: Any, session_meta: dict[str, Any] | None) -> str:
    return str((session_meta or {}).get("output_style") or getattr(config, "output_style", "") or "default")


def _shown(path: Path) -> str:
    """``path`` as the prompt shows it: ``~/…`` under the home directory (shorter, and what a person reads), else
    absolute. The file tools expand ``~``, so the model can pass either form back."""
    home = Path.home().resolve()
    if path == home:
        return "~"
    if path.is_relative_to(home):
        return f"~/{path.relative_to(home).as_posix()}"
    return path.as_posix()


def scope_prompt(cwd: str | Path, add_dirs: list[str] | None = None) -> str:
    """The "Session scope" section: which directory this session works in and what else it may touch.

    Without it the model is never told where it is: it learns its working directory only from tool output, searches
    ``~`` or ``/`` to orient itself and reads unrelated files outside the project, each of which asks (or is refused in
    auto mode). Built only from the cwd, the session's /add-dir directories and the location of ``.git``, so it stays
    the same for the whole session and costs the prompt cache nothing; it never reads a project file."""
    from k3code.memory import project_root

    where = Path(cwd).resolve()
    home = Path.home().resolve()
    extra = [_shown(Path(d).expanduser().resolve()) for d in add_dirs or [] if str(d).strip()]
    head = ["## Session scope", ""]
    if home.is_relative_to(where):  # $HOME itself, or / or /home above it
        kind = "your home directory" if where == home else "a directory that contains your home directory"
        lines = [*head, f"- Working directory: `{_shown(where)}`: {kind}, not a project."]
        if extra:
            lines.append("- Added with /add-dir: " + ", ".join(f"`{d}`" for d in extra))
        lines += [
            "",
            "This session did not start inside a project. Work only on the paths the task names; never search, list "
            "or read the home directory recursively to find context. If the task is about a project it does not "
            "locate, ask which one (the user can start k3code inside it, or /add-dir it).",
        ]
        return "\n".join(lines)
    root = project_root(where)
    shown = _shown(where)
    lines = [*head, f"- Working directory: `{shown}`: this session's project; relative paths and commands start here."]
    if not (root / ".git").exists():
        lines.append("- Not inside a git repository.")
    elif root != where:
        lines.append(
            f"- Git repository root: `{_shown(root)}` (above the working directory: files outside the working "
            "directory are outside this session's scope)."
        )
    if extra:
        lines.append("- Also in scope (added with /add-dir): " + ", ".join(f"`{d}`" for d in extra))
    lines += [
        "",
        "Search and read inside these directories only. Start from what the task names (a file, symbol or error text) "
        "and search from the working directory, never from `~` or `/`. If the task needs something outside them, say "
        "what and where instead of looking around for it: the user can /add-dir it.",
    ]
    return "\n".join(lines)


def prompt_sections(
    base: str,
    *,
    cwd: str | Path,
    config: Any,
    session_meta: dict[str, Any] | None = None,
    mcp: Any = None,
    skills: bool = True,
    advisor: bool = True,
    attended: bool = True,
    session_id: str | None = None,
) -> list[tuple[str, str]]:
    """The prompt as ``(name, text)`` sections in the order they are sent.

    With ``session_id``, the settings are the session's own (``optimizer.session_settings``): a session in the
    treatment arm of a concurrent config experiment gets its memory and skills limits."""
    from k3code.learning.optimizer import session_settings

    config = session_settings(config, session_id)
    # The prompt is sent as one prefix that provider caches match from the top, so the sections that change rarely
    # come first and the learned ones (USER.md's auto section is rewritten by the distiller) come last: a rewrite
    # then invalidates only its own tail. Nothing here may depend on the time, a counter or the turn.
    parts = [("base", base.rstrip())]
    style = outputstyle.style_text(effective_style(config, session_meta), cwd, attended=attended)
    if style:
        parts.append(("style", style))
    from k3code.autonomy import advisor as _advisor
    from k3code.autonomy import autonomy_cfg

    if advisor and _advisor.session_advisor(session_meta, autonomy_cfg(config).get("advisor", "")):  # the tool exists
        parts.append(("advisor", _advisor.PROMPT_NOTE))
    ctx = getattr(config, "context", None) or {}  # M1: prompt-size knobs the optimizer may tune (defaults unchanged)
    if skills and (
        index := skills_prompt(cwd, list(config.skills.roots), limit=int(ctx.get("skill_prompt_limit", PROMPT_LIMIT)))
    ):  # ``skills=False`` for a loop that has no ``skill`` tool to load them with
        parts.append(("skills", index))
    if mcp is not None and (m := mcp_prompt(mcp)):
        parts.append(("mcp", m))
    # where the session works: project-specific like everything after it, and fixed for the session (an /add-dir
    # changes it, rarely), so it sits ahead of the facts and learned sections
    parts.append(("scope", scope_prompt(cwd, (session_meta or {}).get("add_dirs"))))
    from k3code.learning.projectprep import facts_prompt

    if facts := facts_prompt(cwd):  # the stored stack scan; with or without a memory file, changes on re-scan only
        parts.append(("facts", facts))
    from k3code.learning import learning_cfg

    # learning.enabled: false keeps what learning wrote on its own (learned notes, optimizer overlays) out of the
    # prompt too, not only stops new learning; lines a person accepted (gotcha cards, USER.md) stay
    learned = bool(learning_cfg(config)["enabled"])
    if mem := memory_prompt(cwd, limit=memory_limit(config), learned=learned):  # /memory reports the same cap
        parts.append(("memory", mem))
    from k3code.learning.gotchas import gotchas_prompt, user_gotchas_prompt
    from k3code.learning.optimizer import overlay_prompt

    if gotchas := gotchas_prompt(cwd):  # accepted project gotchas; changes only when one is accepted
        parts.append(("gotchas", gotchas))
    if machine := user_gotchas_prompt():  # lines learned alike in several projects; changes only when one is added
        parts.append(("machine-gotchas", machine))

    # a control-arm session of an A/B experiment goes without
    if learned and (overlay := overlay_prompt(session_id)):
        parts.append(("overlay", overlay))
    return parts


def build_system_prompt(
    base: str,
    *,
    cwd: str | Path,
    config: Any,
    session_meta: dict[str, Any] | None = None,
    mcp: Any = None,
    skills: bool = True,
    advisor: bool = True,
    attended: bool = True,
    session_id: str | None = None,
) -> str:
    sections = prompt_sections(
        base,
        cwd=cwd,
        config=config,
        session_meta=session_meta,
        mcp=mcp,
        skills=skills,
        advisor=advisor,
        attended=attended,
        session_id=session_id,
    )
    return "\n\n".join(text for _, text in sections) + "\n"


def subagent_system_prompt(base: str, *, name: str, description: str, body: str, note: str = "") -> str:
    """A sub-agent's prompt: its header, the composed prompt of the session, then the agent type's own text."""
    return f"[agent:{name}] {description}\n\n{base}\n\n{body}{note}"


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokens_of(text: str) -> int:
    """The estimate every budget in k3code uses: about 4 characters per token."""
    return len(text) // 4


def mode_names(project_dir: str | Path | None = None) -> list[str]:
    from k3code.subagents.types import load_agent_types

    return ["interactive", "headless", "plan", *(f"subagent:{n}" for n in sorted(load_agent_types(project_dir)))]


def assemble_mode(mode: str, *, cwd: str | Path, config: Any) -> tuple[str, list[tuple[str, str]]]:
    """The prompt a loop of ``mode`` starts with and its sections, composed from what is on disk (no session, no MCP
    connection: an MCP server's tool names join the interactive prompt only in a running daemon)."""
    base = base_prompt()
    if mode in ("interactive", "headless"):
        sections = prompt_sections(base, cwd=cwd, config=config)
    elif mode == "plan":
        from k3code.autonomy.plan_first import PLAN_ADDENDUM

        sections = prompt_sections(base, cwd=cwd, config=config, skills=False, advisor=False)
        text = "\n\n".join(t for _, t in sections) + "\n" + PLAN_ADDENDUM  # as plan_first appends it
        return text, [*sections, ("plan-addendum", PLAN_ADDENDUM)]
    elif mode.startswith("subagent:"):
        from k3code.subagents.types import load_agent_types

        name = mode.partition(":")[2]
        atype = load_agent_types(cwd).get(name)
        if atype is None:
            raise ValueError(f"unknown sub-agent type {name!r} (one of {', '.join(mode_names(cwd))})")
        session = prompt_sections(base, cwd=cwd, config=config, attended=False)  # as SubagentManager.build_loop
        text = subagent_system_prompt(
            "\n\n".join(t for _, t in session), name=name, description="<task description>", body=atype.prompt
        )
        header = f"[agent:{name}] <task description>"
        return text + "\n", [("agent-header", header), *session, ("agent-type", atype.prompt)]
    else:
        raise ValueError(f"unknown mode {mode!r} (one of {', '.join(mode_names(cwd))})")
    return "\n\n".join(t for _, t in sections) + "\n", sections


def section_table(sections: list[tuple[str, str]], tool_schemas: str = "") -> str:
    rows = [(name, len(text), tokens_of(text)) for name, text in sections]
    if tool_schemas:
        rows.append(("tool schemas", len(tool_schemas), tokens_of(tool_schemas)))
    width = max(len(r[0]) for r in [*rows, ("total", 0, 0)])
    lines = [f"{'section':<{width}}  {'chars':>7}  {'tokens':>7}"]
    lines += [f"{n:<{width}}  {c:>7}  {t:>7}" for n, c, t in rows]
    lines.append(f"{'total':<{width}}  {sum(r[1] for r in rows):>7}  {sum(r[2] for r in rows):>7}")
    return "\n".join(lines)


def tool_schemas_json() -> str:
    from k3code.tools import build_registry

    return json.dumps(
        [{"name": s.name, "description": s.description, "parameters": s.parameters} for s in build_registry().specs()],
        ensure_ascii=False,
        indent=2,
    )


def prompt_fingerprints(config: Any, cwd: str | Path) -> dict[str, Any]:
    """Hash and section sizes per mode, for the debug bundle: which prompt a session ran with, without its text.

    ``hash`` is the first 16 hex digits of the SHA-256: a full 64-digit hash is what the bundle's redaction hides as a
    credential."""
    out: dict[str, Any] = {"base": {"hash": prompt_hash(base_prompt())[:16], "chars": len(base_prompt())}}
    for mode in mode_names(cwd):
        try:
            text, sections = assemble_mode(mode, cwd=cwd, config=config)
        except Exception as e:  # noqa: BLE001  # a broken memory file must not stop the bundle
            out[mode] = {"error": f"{type(e).__name__}: {e}"}
            continue
        out[mode] = {
            "hash": prompt_hash(text)[:16],
            "chars": len(text),
            "tokens": tokens_of(text),
            "sections": {name: len(t) for name, t in sections},
        }
    return out
