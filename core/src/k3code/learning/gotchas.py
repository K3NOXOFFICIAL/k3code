"""Project gotchas: tool failures that keep coming back in one project, kept under ``$K3CODE_HOME/projects/<id>``.

The learning hub records every tool failure as a ``tool_error`` decision. A retry of the failed call that worked next
becomes the row's hint. A signature that carries such a hint is self-verified: with ``learning.auto_gotchas`` on (the
default) it is written as a gotcha line on its own once it shows up AUTO_REPEATS times in a project within WINDOW, and
the session gets a one-line notification (never for a class in NOT_PROJECT, nor a hint the scrubber redacted
something in). A hintless signature, or any signature with the setting off, waits for REPEATS sightings and is proposed
as a card instead. An accepted (or auto-learned) gotcha is appended to ``gotchas.md`` in the project's
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
from k3code.redact import REDACTED, scrub_text

WINDOW = 14 * 86400.0
REPEATS = 3
#: sightings after which a failure with a working retry (its hint) is learned without asking
AUTO_REPEATS = 2
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


def auto_ok(error_class: str, hint: str) -> bool:
    """Whether a recurring failure may become a gotcha without a card: it has a working retry, says something about
    the project, and its hint held nothing the scrubber had to redact (a lesson must never be built on a secret)."""
    return bool(hint) and error_class not in NOT_PROJECT and REDACTED not in hint and scrub_text(hint) == hint


def has_lesson(path: Path, tool: str, signature: str) -> bool:
    """Whether ``path`` already holds a gotcha for this tool and signature (with any hint)."""
    head = " ".join(f"- {tool}: {signature}".split())
    return any(e == head or e.startswith(head + " — ") for e in _entries(path))
