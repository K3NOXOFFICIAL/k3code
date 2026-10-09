"""Project gotchas: tool failures that keep coming back in one project, kept under ``$K3CODE_HOME/projects/<id>``.

The learning hub records every tool failure as a ``tool_error`` decision. When the same signature (see
``k3code.toolerrors``) shows up REPEATS times in a project within WINDOW, it proposes a gotcha line, with a hint from
the call that worked next when there was one. An accepted gotcha is appended to ``gotchas.md`` in the project's
directory under the k3code home (never a file in the repository) and goes into the system prompt as a fenced
"known pitfalls in this project" block of at most MAX_LINES lines, oldest dropped.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from k3code.learning.decisions import project_id
from k3code.paths import home as k3code_home
from k3code.redact import scrub_text

WINDOW = 14 * 86400.0
REPEATS = 3
MAX_LINES = 30
HEADING = "## Known pitfalls in this project"
#: failure classes that say nothing about the project: a user's denial (E6 learns from its reason) and a call the
#: model got wrong (its schema says so)
NOT_PROJECT = frozenset({"denied", "invalid_arguments"})


def project_dir_name(pid: str) -> str:
    """A directory name for a project id (``git:github.com/o/r`` holds ``/`` and ``:``): readable part + hash."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", pid).strip("._")[:80] or "project"
    return f"{safe}-{hashlib.sha1(pid.encode()).hexdigest()[:8]}"


def gotchas_path(pid: str, home: Path | None = None) -> Path:
    return (home or k3code_home()) / "projects" / project_dir_name(pid) / "gotchas.md"


def _entries(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("- ")]


def append_gotcha(path: Path, line: str) -> None:
    """Add ``- line`` to ``path``, keeping the newest MAX_LINES entries."""
    entry = "- " + " ".join(scrub_text(line).split())
    entries = [e for e in _entries(path) if e != entry] + [entry]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(entries[-MAX_LINES:]) + "\n", encoding="utf-8")


def gotchas_prompt(cwd: str | Path, home: Path | None = None) -> str:
    """The fenced block for the system prompt, or "" when the project has no gotchas."""
    root = home or k3code_home()
    if not (root / "projects").is_dir():  # no project ever accepted one: skip the git call behind project_id
        return ""
    entries = _entries(gotchas_path(project_id(cwd), root))[-MAX_LINES:]
    if not entries:
        return ""
    body = "\n".join(entries).replace("```", "'''")
    return f"{HEADING}\nTool failures that kept recurring here, and what worked instead. Avoid them.\n```\n{body}\n```"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_.\-]+", text.lower()))


def followup_hint(tool: str, failed_args: dict[str, Any], ok_args: dict[str, Any]) -> str:
    """One line about the call that worked after a failure, or "" when it does not look like a retry of it."""
    if tool == "bash":
        before, after = str(failed_args.get("command", "")), str(ok_args.get("command", ""))
        if not after or after == before or not (_words(before) & _words(after)):
            return ""
        cmd = next((ln.strip() for ln in after.splitlines() if ln.strip()), "")
        return f"`{scrub_text(cmd)[:120]}` worked instead"
    changed = sorted(k for k in ok_args if ok_args.get(k) != failed_args.get(k))
    return f"worked with different {', '.join(changed)}" if changed else ""


def proposal_text(signature: str, hint: str) -> str:
    return f"project gotcha: {signature}" + (f" — {hint}" if hint else "")
