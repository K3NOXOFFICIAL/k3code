"""Agent types: markdown files with frontmatter (name, description, tools, tier).

Search order (later wins): built-ins shipped in the package, ``$K3CODE_HOME/agents/``, ``<project>/.k3code/agents/``.
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


def parse_agent_md(text: str, default_name: str = "", source: str = "") -> AgentType | None:
    """Parse ``---`` frontmatter (flat ``key: value``) plus the markdown body."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            for line in parts[0].splitlines()[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip().lower()] = v.strip().strip("'\"")
            body = parts[1].lstrip("-").lstrip("\n")
    name = meta.get("name") or default_name
    if not name:
        return None
    tools = [t.strip() for t in meta.get("tools", "").strip("[]").split(",") if t.strip()]
    tier = meta.get("tier", "")
    if tier and tier not in VALID_TIERS:
        logger.warning("agent %s: unknown tier %r ignored", name, tier)
        tier = ""
    return AgentType(
        name=name, description=meta.get("description", ""), tools=tools, tier=tier, prompt=body.strip(), source=source
    )


def _load_dir(path: Path, source: str, into: dict[str, AgentType]) -> None:
    if not path.is_dir():
        return
    for f in sorted(path.glob("*.md")):
        try:
            agent = parse_agent_md(f.read_text(encoding="utf-8"), f.stem, source)
        except OSError:
            continue
        if agent:
            into[agent.name] = agent


def load_agent_types(project_dir: str | Path | None = None, home: str | Path | None = None) -> dict[str, AgentType]:
    home_dir = Path(home) if home else Path(os.environ.get("K3CODE_HOME") or Path.home() / ".k3code")
    out: dict[str, AgentType] = {}
    _load_dir(BUILTIN_DIR, "builtin", out)
    _load_dir(home_dir / "agents", "user", out)
    if project_dir:
        _load_dir(Path(project_dir) / ".k3code" / "agents", "project", out)
    return out
