"""A cheap syntax check of a file right after ``edit`` or ``write``, appended to the tool result.

A syntax error used to surface only when the model ran the tests, one model round trip later (or never, when it did
not run them). The check is the cheapest one at hand for the file type: an in-process parse for Python, JSON, YAML
and TOML; ``node --check`` as a subprocess for JavaScript when node is installed (TypeScript has no cheap check: a
project ``tsc`` run takes seconds). A subprocess gets no provider keys (``child_env``), is killed after CHECK_TIMEOUT
seconds and its output is cut to CHECK_MAX_CHARS. A file over CHECK_MAX_BYTES is not checked. A check that cannot run
says nothing: it is feedback for the model, never a gate on the edit (the file is written either way).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from k3code.reliability.sandbox import child_env

logger = logging.getLogger(__name__)

#: seconds a checker subprocess may take
CHECK_TIMEOUT = 5.0
#: chars of a checker's output the model sees
CHECK_MAX_CHARS = 600
#: files larger than this are not checked (a parse of a generated 20 MB JSON would hold the event loop's thread)
CHECK_MAX_BYTES = 1_000_000

_PY = frozenset({".py", ".pyi"})
_JS = frozenset({".js", ".mjs", ".cjs"})
_JSON = frozenset({".json"})
_YAML = frozenset({".yaml", ".yml"})
_TOML = frozenset({".toml"})
#: every type a check exists for
CHECKED_SUFFIXES = _PY | _JS | _JSON | _YAML | _TOML


def enabled(config: Any) -> bool:
    """``tools.syntax_check`` (default on)."""
    tools = getattr(config, "tools", None)
    return bool(tools.get("syntax_check", True)) if isinstance(tools, dict) else True


def _cut(text: str, limit: int = CHECK_MAX_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def parse_check(path: Path, text: str) -> str | None:
    """In-process parse: "" when ``text`` parses as its type, the error (with its line) when not, None for a type
    without an in-process parser."""
    ext = path.suffix.lower()
    try:
        if ext in _PY:
            ast.parse(text, filename=str(path))
        elif ext in _JSON:
            json.loads(text)
        elif ext in _YAML:
            import yaml

            yaml.safe_load(text)
        elif ext in _TOML:
            import tomllib

            tomllib.loads(text)
        else:
            return None
    except SyntaxError as e:
        where = f"line {e.lineno}" + (f", column {e.offset}" if e.offset else "")
        return f"{e.msg} ({where})"
    except Exception as e:  # noqa: BLE001 - whatever the parser says is the finding (its message names the line)
        mark = getattr(e, "problem_mark", None)  # yaml: the message repeats the whole context; the mark has the line
        if mark is not None and getattr(e, "problem", None):
            return f"{e.problem} (line {mark.line + 1}, column {mark.column + 1})"
        return _cut(" ".join(str(e).split()))
    return ""


async def _node_check(path: Path) -> str | None:
    """``node --check``: "" when the file parses, its message when not, None when node is missing or did not finish."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            node,
            "--check",
            str(path),
            cwd=path.parent,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=child_env(),
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), CHECK_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, 9)
        await proc.wait()
        logger.info("node --check of %s timed out after %.0fs: skipped", path.name, CHECK_TIMEOUT)
        return None
    if proc.returncode == 0:
        return ""
    # node prints the offending line, a caret line and then "SyntaxError: ..."; the error line is what matters
    lines = [ln for ln in out.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    said = next((ln for ln in lines if "Error" in ln), lines[-1] if lines else "node --check failed")
    where = next(
        (ln.rpartition(":")[2] for ln in lines if ln.startswith(str(path)) and ln.rpartition(":")[2].isdigit()), ""
    )
    return _cut(said + (f" (line {where})" if where else ""))


async def syntax_check(path: Path) -> str | None:
    """The finding of the cheapest syntax check for ``path``: "" when it passes, the (cut) error when it fails, None
    when no check applies or none could run."""
    if path.suffix.lower() not in CHECKED_SUFFIXES:
        return None
    try:
        if path.stat().st_size > CHECK_MAX_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if path.suffix.lower() in _JS:
        return await _node_check(path)
    return await asyncio.to_thread(parse_check, path, text)


def render(finding: str | None) -> str:
    """The line appended to an edit or write result: nothing when no check ran."""
    if finding is None:
        return ""
    if finding == "":
        return "syntax: ok"
    return f"syntax: {finding}. Fix it before you go on."
