"""Agent types: markdown files with frontmatter (name, description, tools, tier).

Search order: built-ins shipped in the package, then ``$K3CODE_HOME/agents/`` (a user agent replaces a built-in of
the same name), then ``<project>/.k3code/agents/`` only when the project is trusted, and only to add new names.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent / "builtin"
VALID_TIERS = ("main", "strong", "cheap", "fast")


@dataclass
class AgentType:
    name: str
    description: str = ""
    #: allowed tool names; empty = all tools
    tools: list[str] = field(default_factory=list)
    #: main | strong | cheap | fast; empty = the task-kind policy decides
    tier: str = ""
    prompt: str = ""
    source: str = "builtin"


def _unquote(value: str) -> str:
    return value.strip().strip("'\"").strip()


def parse_agent_md(text: str, default_name: str = "", source: str = "") -> AgentType | None:
    """Parse ``---`` frontmatter (flat ``key: value``, and ``- item`` lines under a key) plus the markdown body.

    None (with a warning) for a file that cannot be trusted to say what the agent may do: frontmatter that is never
    closed, or a ``tools:`` key that names no tool. An agent's ``tools`` empty means ALL tools, so a read-only agent
    whose list was written as YAML lines (``  - read``) or lost to a typo must not come out with every tool."""
    meta: dict[str, str] = {}
    items: dict[str, list[str]] = {}
    body = text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            logger.warning("agent %s: frontmatter has no closing '---'; skipped", default_name or source)
            return None
        key = ""
        for line in lines[1:end]:
            if line.lstrip().startswith("- ") and key:
                items.setdefault(key, []).append(_unquote(line.lstrip()[2:]))
            elif ":" in line:
                k, _, v = line.partition(":")
                key = k.strip().lower()
                meta[key] = v.strip().strip("'\"")
        body = "\n".join(lines[end + 1 :])
    name = meta.get("name") or default_name
    if not name:
        return None
    tools = [t for t in (_unquote(t) for t in meta.get("tools", "").strip("[]").split(",")) if t]
    tools += [t for t in items.get("tools", []) if t]
    if "tools" in meta and not tools:
        logger.warning("agent %s: 'tools:' names no tool (an agent without the key gets all of them); skipped", name)
        return None
    tier = meta.get("tier", "")
    if tier and tier not in VALID_TIERS:
        logger.warning("agent %s: unknown tier %r ignored", name, tier)
        tier = ""
    return AgentType(
        name=name, description=meta.get("description", ""), tools=tools, tier=tier, prompt=body.strip(), source=source
    )


def _load_dir(path: Path, source: str, into: dict[str, AgentType], *, add_only: bool = False) -> None:
    if not path.is_dir():
        return
    for f in sorted(path.glob("*.md")):
        try:
            agent = parse_agent_md(f.read_text(encoding="utf-8"), f.stem, source)
        except OSError:
            continue
        if not agent:
            continue
        if add_only and agent.name in into:
            logger.warning(
                "project agent %s (%s) skipped: it would replace the %s agent of that name",
                agent.name,
                f,
                into[agent.name].source,
            )
            continue
        into[agent.name] = agent


def load_agent_types(project_dir: str | Path | None = None, home: str | Path | None = None) -> dict[str, AgentType]:
    """Built-in, then user agents; a trusted project may add agents but never replace one (see k3code.trust)."""
    from k3code import trust

    home_dir = Path(home) if home else Path(os.environ.get("K3CODE_HOME") or Path.home() / ".k3code")
    out: dict[str, AgentType] = {}
    _load_dir(BUILTIN_DIR, "builtin", out)
    _load_dir(home_dir / "agents", "user", out)
    if project_dir and trust.content_allowed(project_dir):
        _load_dir(Path(project_dir) / ".k3code" / "agents", "project", out, add_only=True)
    return out
