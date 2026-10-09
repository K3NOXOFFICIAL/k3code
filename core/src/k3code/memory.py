"""Memory files: project (K3CODE.md → AGENTS.md fallback) and user (``$K3CODE_HOME/memory/USER.md``)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from k3code.paths import home

PROJECT_FILES = ("K3CODE.md", "AGENTS.md")
MAX_MEMORY_CHARS = 20_000  # per file, as injected into the prompt


def project_root(cwd: str | Path) -> Path:
    """Nearest ancestor of ``cwd`` holding ``.git`` (the repo root), else ``cwd`` itself."""
    start = Path(cwd).resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def user_memory_path() -> Path:
    return home() / "memory" / "USER.md"


def project_memory_path(cwd: str | Path, *, for_write: bool = False) -> Path:
    """``K3CODE.md`` if present, else ``AGENTS.md`` if present, else (for writes) ``K3CODE.md``."""
    root = project_root(cwd)
    for name in PROJECT_FILES:
        if (root / name).is_file():
            return root / name
    return root / PROJECT_FILES[0]


@dataclass
class MemoryFile:
    scope: str  # "user" | "project"
    path: Path
    text: str


def load_memory(cwd: str | Path) -> list[MemoryFile]:
    """Existing memory files in prompt order: user first, then project (more specific, later)."""
    out: list[MemoryFile] = []
    for scope, path in (("user", user_memory_path()), ("project", project_memory_path(cwd))):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                out.append(MemoryFile(scope, path, text))
    return out


def fenced(label: str, body: str) -> str:
    """``label`` and ``body`` in a code fence longer than any backtick run inside it, so the body cannot close it."""
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{label}\n{fence}text\n{body}\n{fence}"


def memory_prompt(cwd: str | Path, limit: int = MAX_MEMORY_CHARS) -> str:
    """User memory as written; the repository's memory file fenced as what it is (text from the repository)."""
    parts = []
    for m in load_memory(cwd):
        body = m.text if len(m.text) <= limit else m.text[:limit] + "\n…(truncated)"
        if m.scope == "user":
            parts.append(f"## User memory\n\n{body}")
        else:
            parts.append(
                f"## Project memory ({m.path.name})\n\n" + fenced("project instructions (from the repository):", body)
            )
    return "\n\n".join(parts)


def append_memory(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + sep + f"- {text.strip()}\n", encoding="utf-8")
