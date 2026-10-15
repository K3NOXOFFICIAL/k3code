"""What a ``write`` or ``edit`` would change, for its approval prompt (issue #131).

The prompt used to show only ``edit <path>``: a person approved file changes without seeing them. The preview is a
unified diff (for ``edit`` computed by the edit tool's own replacement, so it is what will be written), capped at
:data:`MAX_LINES` lines. Nothing is written here.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from k3code.tools import _resolve_path, plan_edit

#: Lines of a preview; the rest is counted in a last line.
MAX_LINES = 200
#: Characters kept of one preview line (a minified bundle is one line).
MAX_LINE_CHARS = 400
#: Files larger than this are not read for a preview.
MAX_FILE_BYTES = 2_000_000


def change_preview(tool_name: str, arguments: dict[str, Any], cwd: Path | str | None = None) -> str | None:
    """The preview for ``tool_name`` called with ``arguments``, or None for any other tool (or bad arguments)."""
    base = Path(cwd) if cwd is not None else None
    try:
        if tool_name == "edit":
            return _edit_preview(arguments, base)
        if tool_name == "write":
            return _write_preview(arguments, base)
    except (KeyError, TypeError, OSError) as exc:  # a malformed call: the tool itself will report it
        return f"(no preview: {type(exc).__name__}: {exc})"
    return None


def _shown(path: Path, base: Path | None) -> str:
    if base is not None:
        try:
            return path.relative_to(base.resolve()).as_posix()
        except ValueError:
            pass
    return str(path)


def _diff(before: str, after: str, name: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile=f"a/{name}", tofile=f"b/{name}", n=3, lineterm=""
        )
    )
    return _cap(lines) if lines else "(no change)"


def _cap(lines: list[str]) -> str:
    shown = [ln if len(ln) <= MAX_LINE_CHARS else ln[:MAX_LINE_CHARS] + " …" for ln in lines[:MAX_LINES]]
    if len(lines) > MAX_LINES:
        shown.append(f"… {len(lines) - MAX_LINES} more lines")
    return "\n".join(shown)


def _too_big(path: Path) -> str | None:
    size = path.stat().st_size
    return f"(file of {size} bytes: too large to preview)" if size > MAX_FILE_BYTES else None


def _edit_preview(arguments: dict[str, Any], base: Path | None) -> str:
    path = _resolve_path(arguments["path"], base)
    if path.is_file() and (big := _too_big(path)):
        return big
    plan = plan_edit(arguments, cwd=base)
    if isinstance(plan, dict):
        first = str(plan.get("error", "")).splitlines()[0] if plan.get("error") else "unknown error"
        return f"(this edit will fail: {first})"
    return _diff(plan.before, plan.after, _shown(plan.path, base))


def _write_preview(arguments: dict[str, Any], base: Path | None) -> str:
    path = _resolve_path(arguments["path"], base)
    content = str(arguments["content"])
    name = _shown(path, base)
    if not path.exists():
        lines = content.splitlines()
        return _cap([f"new file {name}: {len(lines)} lines", *(f"+{ln}" for ln in lines)])
    if not path.is_file():
        return f"({name} is not a regular file)"
    if big := _too_big(path):
        return big
    try:
        before = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return f"(replaces {name}, which is not UTF-8 text, {path.stat().st_size} bytes)"
    return _diff(before.replace("\r\n", "\n"), content.replace("\r\n", "\n"), name)
