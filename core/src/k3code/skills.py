"""Skills: directories with a ``SKILL.md`` (frontmatter ``name`` + ``description``)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from k3code.paths import home

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
MAX_DEPTH = 4
PROMPT_LIMIT = 60  # skills named in the system prompt; the rest are reachable via the skill tool


@dataclass
class Skill:
    name: str
    description: str
    path: Path

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")


def parse_frontmatter(text: str) -> dict[str, str]:
    m = _FM.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def skill_roots(cwd: str | Path, extra: list[str] | None = None) -> list[Path]:
    roots = [home() / "skills", Path(cwd) / ".k3code" / "skills", *(Path(r).expanduser() for r in extra or [])]
    return [r for r in roots if r.is_dir()]


def _find(root: Path) -> list[Path]:
    found: list[Path] = []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        marker = d / "SKILL.md"
        if marker.is_file():
            found.append(marker)
            continue
        if depth >= MAX_DEPTH:
            continue
        try:
            kids = [c for c in sorted(d.iterdir()) if c.is_dir() and not c.name.startswith((".", "_"))]
            stack.extend((c, depth + 1) for c in kids)
        except OSError:
            continue
    return sorted(found)


def discover(cwd: str | Path, extra_roots: list[str] | None = None) -> list[Skill]:
    """All skills; earlier roots win on a name clash (user < project < config roots by order given)."""
    seen: dict[str, Skill] = {}
    for root in skill_roots(cwd, extra_roots):
        for marker in _find(root):
            try:
                head = marker.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            fm = parse_frontmatter(head)
            name = fm.get("name") or marker.parent.name
            seen.setdefault(name, Skill(name, " ".join(fm.get("description", "").split()), marker))
    return sorted(seen.values(), key=lambda s: s.name)


def find_skill(name: str, cwd: str | Path, extra_roots: list[str] | None = None) -> Skill | None:
    for s in discover(cwd, extra_roots):
        if s.name == name:
            return s
    return None


def search(query: str, cwd: str | Path, extra_roots: list[str] | None = None, limit: int = 10) -> list[Skill]:
    words = [w for w in re.split(r"\W+", query.lower()) if w]
    scored = []
    for s in discover(cwd, extra_roots):
        hay = f"{s.name} {s.description}".lower()
        score = sum(hay.count(w) + (3 if w in s.name.lower() else 0) for w in words)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda t: (-t[0], t[1].name))
    return [s for _, s in scored[:limit]]


def skills_prompt(cwd: str | Path, extra_roots: list[str] | None = None, limit: int = PROMPT_LIMIT) -> str:
    """Names + descriptions only; full text is loaded on demand via the ``skill`` tool."""
    skills = discover(cwd, extra_roots)
    if not skills:
        return ""
    lines = ["## Skills", "", "Load a skill's full instructions with the `skill` tool (`name`, or `query` to search)."]
    for s in skills[:limit]:
        desc = s.description if len(s.description) <= 110 else s.description[:107] + "..."
        lines.append(f"- {s.name}: {desc}")
    if len(skills) > limit:
        lines.append(f"- …and {len(skills) - limit} more; use `skill` with a `query` to find them.")
    return "\n".join(lines)
