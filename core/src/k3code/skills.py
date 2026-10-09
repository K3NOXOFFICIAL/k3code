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


#: Skill directories a project may ship besides ``.k3code/skills``: Claude Code's and the cross-agent convention.
PROJECT_SKILL_DIRS = (".k3code/skills", ".claude/skills", ".agents/skills")


def claude_user_skills() -> Path:
    return Path.home() / ".claude" / "skills"


def import_claude_enabled() -> bool:
    """``skills.import_claude`` (default true) from the user's config: also load ``~/.claude/skills``."""
    from k3code.config import load_user_section

    section = load_user_section("skills")
    value = section.get("import_claude", True) if isinstance(section, dict) else True
    return value is not False


def skill_roots(cwd: str | Path, extra: list[str] | None = None) -> list[Path]:
    """The user's skills (``$K3CODE_HOME/skills``, then ``~/.claude/skills`` unless ``skills.import_claude`` is
    false), the project's (only once the project is trusted, see k3code.trust), then config roots."""
    from k3code import trust

    user = [home() / "skills", *([claude_user_skills()] if import_claude_enabled() else [])]
    project = [Path(cwd) / d for d in PROJECT_SKILL_DIRS] if trust.content_allowed(cwd) else []
    roots, seen = [], set()
    for r in (*user, *project, *(Path(r).expanduser() for r in extra or [])):
        key = r.resolve() if r.is_dir() else None
        if key is not None and key not in seen:
            seen.add(key)
            roots.append(r)
    return roots


def find_markers(root: Path) -> list[Path]:
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


#: root → (signature of its SKILL.md files, the skills parsed from them). discover() runs on every prompt build and
#: at every turn end; with the cache a root is only walked (directory listings and stats), not read and parsed again.
_CACHE: dict[Path, tuple[tuple[tuple[str, int, int], ...], list[Skill]]] = {}


def _signature(markers: list[Path]) -> tuple[tuple[str, int, int], ...]:
    """Each SKILL.md with its mtime and size: an edit in place changes the file's mtime but not its directory's."""
    sig = []
    for m in markers:
        try:
            st = m.stat()
            sig.append((str(m), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(m), -1, -1))
    return tuple(sig)


def _root_skills(root: Path) -> list[Skill]:
    markers = find_markers(root)
    sig = _signature(markers)
    hit = _CACHE.get(root)
    if hit is not None and hit[0] == sig:
        return hit[1]
    found: list[Skill] = []
    for marker in markers:
        try:
            head = marker.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        fm = parse_frontmatter(head)
        name = fm.get("name") or marker.parent.name
        found.append(Skill(name, " ".join(fm.get("description", "").split()), marker))
    _CACHE[root] = (sig, found)
    return found


def discover(cwd: str | Path, extra_roots: list[str] | None = None) -> list[Skill]:
    """All skills; earlier roots win on a name clash (user < project < config roots by order given)."""
    seen: dict[str, Skill] = {}
    for root in skill_roots(cwd, extra_roots):
        for skill in _root_skills(root):
            seen.setdefault(skill.name, skill)
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


RECENT_USE_DAYS = 30


def rank_skills(skills: list[Skill], cwd: str | Path, *, now: float | None = None) -> list[Skill]:
    """Best first: skills that fit the project's detected stacks (or that the user pinned for it), then skills used
    successfully in the last RECENT_USE_DAYS days (curator usage), then by name. Coarse on purpose: a use does not
    reorder anything unless it crosses one of these lines, so the prompt stays cache-stable."""
    import time

    from k3code.learning import projectstate, recipes
    from k3code.learning.curator import load_usage

    state = projectstate.load(cwd)
    keywords = recipes.keywords_for({str(s.get("id")) for s in state.get("stacks") or [] if isinstance(s, dict)})
    pinned = set((state.get("accepted") or {}).get("skills") or [])
    usage = load_usage()
    cutoff = (now if now is not None else time.time()) - RECENT_USE_DAYS * 86400

    def recent_ok(name: str) -> bool:
        u = usage.get(name) if isinstance(usage, dict) else None
        if not isinstance(u, dict):
            return False
        ok = int(u.get("uses") or 0) - int(u.get("failures") or 0) > 0
        return ok and float(u.get("last_used") or 0) >= cutoff

    def fits(s: Skill) -> bool:
        return s.name in pinned or (bool(keywords) and recipes.skill_score(s.name, s.description, keywords) >= 2)

    return sorted(skills, key=lambda s: (not fits(s), not recent_ok(s.name), s.name))


def skills_prompt(cwd: str | Path, extra_roots: list[str] | None = None, limit: int = PROMPT_LIMIT) -> str:
    """Names + descriptions only; full text is loaded on demand via the ``skill`` tool.

    With more than ``limit`` skills, which ones are named is decided by rank (:func:`rank_skills`); the named ones
    are listed by name, so the section changes only when the chosen set does."""
    skills = discover(cwd, extra_roots)
    if not skills:
        return ""
    lines = ["## Skills", "", "Load a skill's full instructions with the `skill` tool (`name`, or `query` to search)."]
    chosen = sorted(rank_skills(skills, cwd)[:limit], key=lambda s: s.name) if len(skills) > limit else skills
    for s in chosen:
        desc = s.description if len(s.description) <= 110 else s.description[:107] + "..."
        lines.append(f"- {s.name}: {desc}")
    if len(skills) > limit:
        lines.append(f"- …and {len(skills) - limit} more; use `skill` with a `query` to find them.")
    return "\n".join(lines)
