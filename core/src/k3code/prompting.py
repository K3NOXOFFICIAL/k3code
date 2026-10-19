"""System prompt composer: base prompt + output style + memory + skills index + deferred MCP names."""

from __future__ import annotations

import functools
import hashlib
import json
import re
import time
from collections.abc import Callable, Collection
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
    """``path`` as the prompt shows it, quoted: ``~/…`` under the home directory (shorter, and what a person reads),
    else absolute; the file tools expand ``~``, so the model can pass either form back.

    A directory name is not trusted text (an unpacked archive or a cloned repo chooses it) and the system prompt
    outranks tool output, so a name that could end the quote or start a line of its own (a backtick, a newline or
    another control character) is shown JSON-escaped in double quotes instead of between backticks."""
    home = Path.home().resolve()
    if path == home:
        text = "~"
    elif path.is_relative_to(home):
        text = f"~/{path.relative_to(home).as_posix()}"
    else:
        text = path.as_posix()
    if "`" in text or any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 or c in "\u2028\u2029" for c in text):
        return json.dumps(text)
    return f"`{text}`"


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
        lines = [*head, f"- Working directory: {_shown(where)}: {kind}, not a project."]
        if extra:
            lines.append("- Added with /add-dir: " + ", ".join(extra))
        lines += [
            "",
            "This session did not start inside a project. Work only on the paths the task names; never search, list "
            "or read the home directory recursively to find context. If the task is about a project it does not "
            "locate, ask which one (the user can start k3code inside it, or /add-dir it).",
        ]
        return "\n".join(lines)
    root = project_root(where)
    shown = _shown(where)
    lines = [*head, f"- Working directory: {shown}: this session's project; relative paths and commands start here."]
    if not (root / ".git").exists():
        lines.append("- Not inside a git repository.")
    elif root != where:
        lines.append(
            f"- Git repository root: {_shown(root)} (above the working directory: files outside the working "
            "directory are outside this session's scope)."
        )
    if extra:
        lines.append("- Also in scope (added with /add-dir): " + ", ".join(extra))
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
    frozen: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """The prompt as ``(name, text)`` sections in the order they are sent.

    With ``session_id``, the settings are the session's own (``optimizer.session_settings``): a session in the
    treatment arm of a concurrent config experiment gets its memory and skills limits.

    ``frozen`` is a session's snapshot (``LiveSession.prompt_frozen``) of what changes under a session without the
    user asking: the custom style text, the skills index, the MCP section and the learned tail. Each is kept in a slot
    of its own with the inputs it was taken for; a slot that is empty or was taken for other inputs is filled now, and
    a loop that does not show a section leaves its slot alone."""
    from k3code.learning.optimizer import session_settings

    config = session_settings(config, session_id)
    # The prompt is sent as one prefix that provider caches match from the top, so the sections that change rarely
    # come first and the learned ones (USER.md's auto section is rewritten by the distiller) come last: a rewrite
    # then invalidates only its own tail. Nothing here may depend on the time, a counter or the turn.
    parts = [("base", base.rstrip())]
    name, where = effective_style(config, session_meta), str(Path(cwd).resolve())
    # a custom style is a file the user may edit mid-session: its text is the session's snapshot (a preset is fixed)
    custom = _frozen(frozen, "style", [name, where], lambda: outputstyle.custom_text(name, cwd))
    style = custom if custom is not None else outputstyle.preset_text(name, attended=attended)
    if style:
        parts.append(("style", style))
    from k3code.autonomy import advisor as _advisor
    from k3code.autonomy import autonomy_cfg

    if advisor and _advisor.session_advisor(session_meta, autonomy_cfg(config).get("advisor", "")):  # the tool exists
        parts.append(("advisor", _advisor.PROMPT_NOTE))
    ctx = getattr(config, "context", None) or {}  # M1: prompt-size knobs the optimizer may tune (defaults unchanged)
    roots, limit = list(config.skills.roots), int(ctx.get("skill_prompt_limit", PROMPT_LIMIT))
    # ``skills=False`` for a loop that has no ``skill`` tool to load them with; the ranking above the cap reads the
    # clock once, when the snapshot is taken
    if skills and (
        index := _frozen(
            frozen, "skills", [where, roots, limit], lambda: skills_prompt(cwd, roots, limit=limit, now=time.time())
        )
    ):
        parts.append(("skills", index))
    # the tools of the servers connected when the snapshot was taken: a server that connects later is found through
    # ``mcp_tool_search`` and named here after the next refresh
    if mcp is not None and (m := _frozen(frozen, "mcp", [], lambda: mcp_prompt(mcp))):
        parts.append(("mcp", m))
    # where the session works: project-specific like everything after it, and fixed for the session (an /add-dir
    # changes it, rarely), so it sits ahead of the facts and learned sections
    parts.append(("scope", scope_prompt(cwd, (session_meta or {}).get("add_dirs"))))
    return parts + frozen_learned_sections(frozen, cwd=cwd, config=config, session_id=session_id)


def learned_sections(cwd: str | Path, config: Any, session_id: str | None = None) -> list[tuple[str, str]]:
    """The prompt's tail: what learning and the user's memory files put in it (stack facts, memory files, learned
    notes, gotchas, the optimizer overlay), read from disk now. ``config`` is the session's own settings."""
    parts: list[tuple[str, str]] = []
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


def frozen_learned_sections(
    frozen: dict[str, Any] | None, *, cwd: str | Path, config: Any, session_id: str | None = None
) -> list[tuple[str, str]]:
    """``learned_sections``, kept in ``frozen`` for a session: its background writers (the review at user turn 7,
    13, ..., the daily distill of USER.md, a re-scan, an overlay) rewrite those files mid-session, and a prompt read
    from disk every turn then changed its prefix and missed the provider's cache. The snapshot is taken at the first
    prompt and kept until the session clears it (``LiveSession.refresh_prompt_inputs``: compaction, /clear, a
    project move, /memory add) or the inputs it was taken for change (the directory, learning on or off, the cap)."""
    from k3code.learning import learning_cfg

    key = [str(Path(cwd).resolve()), bool(learning_cfg(config)["enabled"]), memory_limit(config), session_id or ""]
    return list(_frozen(frozen, "learned", key, lambda: learned_sections(cwd, config, session_id)))


def _frozen(frozen: dict[str, Any] | None, slot: str, key: list[Any], make: Callable[[], Any]) -> Any:
    """``make()``, kept in ``frozen[slot]`` for the inputs ``key``: the same value until the session clears the slot
    (``LiveSession.refresh_prompt_inputs``) or asks for other inputs. No snapshot (``frozen`` None): ``make()`` now."""
    if frozen is None:
        return make()
    hit = frozen.get(slot)
    if hit is None or hit[0] != key:
        value = make()
        if slot == "mcp" and not value:
            return value  # no server is connected yet: keep looking, a snapshot of nothing would hide it for good
        hit = frozen[slot] = (key, value)
    return hit[1]


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
    frozen: dict[str, Any] | None = None,
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
        frozen=frozen,
    )
    return "\n\n".join(text for _, text in sections) + "\n"


#: The heading of the base prompt's last section: every line of the base that names a tool is in it (a test pins that)
TOOL_USE_HEADING = "## Tool Use"
_TOOL_REF = re.compile(r"`([a-z_]+)`")


@functools.cache
def _known_tools() -> frozenset[str]:
    """Every tool name the base prompt can mention: the registry's, plus the ones a loop registers on top of it."""
    from k3code.tools import build_registry

    return frozenset(build_registry().names()) | {"skill", "task", "task_result", "advisor"}


def split_tool_use(base: str) -> tuple[str, str]:
    """``base`` as (everything before its tool-use section, that section); the section is "" when there is none."""
    at = base.find(f"\n{TOOL_USE_HEADING}\n")
    return (base, "") if at < 0 else (base[: at + 1], base[at + 1 :])


def tool_use_for(section: str, tools: Collection[str]) -> str:
    """``section`` without the lines that name a tool outside ``tools``; "" when nothing is left under its heading."""
    kept = [ln for ln in section.splitlines() if all(n in tools for n in _TOOL_REF.findall(ln) if n in _known_tools())]
    return "\n".join(kept).strip() if sum(1 for ln in kept if ln.strip()) > 1 else ""


def subagent_sections(
    base: str,
    *,
    name: str,
    description: str,
    body: str,
    tools: Collection[str],
    cwd: str | Path,
    config: Any,
    session_meta: dict[str, Any] | None = None,
    session_id: str | None = None,
    note: str = "",
) -> list[tuple[str, str]]:
    """A sub-agent's prompt as ``(name, text)`` sections, with what its types share first.

    The provider's cache matches a prompt from the top, so the base (without its tool-use section) and the session's own
    sections come first and are the same for every child of a session. What follows differs by type: the tool-use lines
    for the tools the child has, the skills index when it has the ``skill`` tool to load them, the type's own text; the
    task, which differs per child, is last. A child never hears of a tool it does not have: it has no MCP tools and no
    advisor, so neither's note is in its prompt either."""
    head, tool_use = split_tool_use(base)
    sections = prompt_sections(
        head,
        cwd=cwd,
        config=config,
        session_meta=session_meta,
        mcp=None,
        skills="skill" in tools,
        advisor=False,
        attended=False,  # a child's output goes to its parent, not to a person who could fill in a TODO(human)
        session_id=session_id,
    )
    skills = [s for s in sections if s[0] == "skills"]
    sections = [s for s in sections if s[0] != "skills"]
    if text := tool_use_for(tool_use, tools):
        sections.append(("tools", text))
    return [*sections, *skills, ("agent-type", body), ("agent-task", f"[agent:{name}] {description}{note}")]


def subagent_system_prompt(base: str, **kwargs: Any) -> str:
    return "\n\n".join(text for _, text in subagent_sections(base, **kwargs)) + "\n"


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
        tools = set(atype.tools) or {*_known_tools()}  # a type without a tools list gets them all
        sections = subagent_sections(  # as SubagentManager.build_loop
            base, name=name, description="<task description>", body=atype.prompt, tools=tools, cwd=cwd, config=config
        )
        return "\n\n".join(t for _, t in sections) + "\n", sections
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
