"""Tool registry and implementations."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from k3code.providers.types import ToolSpec
from k3code.tools.fuzzy_match import (
    format_no_match_hint,
    fuzzy_find_and_replace,
)

# ── Tool registry ──────────────────────────────────────────────────────


class ToolRegistry:
    """Registry of available tools with JSON-schema specs."""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, callable]] = {}
        self._deferred: set[str] = set()  # registered + callable, but schema hidden until activated
        self._active: set[str] = set()

    def register(self, spec: ToolSpec, handler: callable, *, deferred: bool = False) -> None:
        self._tools[spec.name] = (spec, handler)
        if deferred:
            self._deferred.add(spec.name)
        else:
            self._deferred.discard(spec.name)

    def activate(self, names: list[str]) -> None:
        """Start advertising the schemas of deferred tools (``mcp_tool_search``)."""
        self._active.update(n for n in names if n in self._deferred)

    def get(self, name: str) -> tuple[ToolSpec, callable] | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [spec for n, (spec, _) in self._tools.items() if n not in self._deferred or n in self._active]

    def names(self) -> list[str]:
        return list(self._tools.keys())


# ── Tool implementations ───────────────────────────────────────────────


def _resolve_path(path: str, cwd: Path | None = None) -> Path:
    """Resolve a path against the session cwd (never the process cwd) and expand ~.

    Whether the path may be touched is decided by the permission engine
    (project roots, add-dirs, approvals), not here.
    """
    base = cwd or Path.cwd()
    p = Path(path).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


async def tool_read(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Read a file, optionally with line ranges."""
    path = _resolve_path(arguments["path"], cwd)
    if not path.is_file():
        return {"error": f"File not found: {path}"}
    start = arguments.get("start", 1)
    end = arguments.get("end")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if end is None:
        end = len(lines)
    start = max(1, min(start, len(lines) + 1))
    end = max(start, min(end, len(lines)))
    selected = lines[start - 1 : end]
    return {"content": "\n".join(selected), "lines": f"{start}-{end} of {len(lines)}"}


async def tool_write(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Write a file, creating parent directories."""
    path = _resolve_path(arguments["path"], cwd)
    content = arguments["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path)}


async def tool_edit(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Edit a file using fuzzy find-and-replace."""
    path = _resolve_path(arguments["path"], cwd)
    if not path.is_file():
        return {"error": f"File not found: {path}"}
    old_string = arguments["old_string"]
    new_string = arguments["new_string"]
    replace_all = arguments.get("replace_all", False)
    content = path.read_text(encoding="utf-8", errors="replace")
    new_content, count, strategy, error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all=replace_all
    )
    if error:
        hint = format_no_match_hint(error, count, old_string, content)
        return {"error": error + hint}
    path.write_text(new_content, encoding="utf-8")
    return {"ok": True, "replacements": count, "strategy": strategy}


async def tool_bash(
    arguments: dict[str, Any], *, cwd: Path | None = None, sandbox: list[str] | None = None
) -> dict[str, Any]:
    """Run a shell command with timeout and process-group kill.

    ``sandbox`` is a bwrap argv prefix (see ``reliability.sandbox``); the command then runs inside it.
    """
    cmd = arguments["command"]
    timeout = arguments.get("timeout", 30.0)
    workdir = _resolve_path(arguments.get("cwd", "."), cwd)
    # Security: only allow a reasonable subset; shell=True for pipes/redirects
    try:
        if sandbox:
            proc = await asyncio.create_subprocess_exec(
                *sandbox,
                "/bin/sh",
                "-c",
                cmd,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid,  # new process group for kill
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid,  # new process group for kill
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                os.killpg(proc.pid, 15)  # SIGTERM to the group
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                os.killpg(proc.pid, 9)  # SIGKILL
            stdout, stderr = await proc.communicate()
            return {
                "error": f"Command timed out after {timeout}s",
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": -1,
            }
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        # Truncate large outputs
        max_chars = 10000
        if len(stdout_text) > max_chars:
            stdout_text = stdout_text[:max_chars] + f"\n... [truncated {len(stdout_text) - max_chars} chars]"
        if len(stderr_text) > max_chars:
            stderr_text = stderr_text[:max_chars] + f"\n... [truncated {len(stderr_text) - max_chars} chars]"
        return {
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": proc.returncode,
        }
    except Exception as e:
        return {"error": f"Failed to execute: {e}"}


async def tool_grep(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Search for a pattern using ripgrep or Python fallback."""
    pattern = arguments["pattern"]
    path = _resolve_path(arguments.get("path", "."), cwd)
    include = arguments.get("include")
    exclude = arguments.get("exclude")
    try:
        cmd = ["rg", "--line-number", "--no-heading", "--color=never"]
        if include:
            for inc in (include if isinstance(include, list) else [include]):
                cmd += ["-g", inc]
        if exclude:
            for exc in (exclude if isinstance(exclude, list) else [exclude]):
                cmd += ["-g", f"!{exc}"]
        cmd += [pattern, str(path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode not in (0, 1):  # 1 = no matches
            raise RuntimeError(stderr.decode())
        return {"matches": stdout.decode("utf-8", errors="replace").strip()}
    except (FileNotFoundError, RuntimeError):
        # Python fallback
        import re
        matches = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                if include and not any(Path(f).match(p) for p in (include if isinstance(include, list) else [include])):
                    continue
                if exclude and any(Path(f).match(p) for p in (exclude if isinstance(exclude, list) else [exclude])):
                    continue
                fp = Path(root) / f
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                    for i, line in enumerate(text.splitlines(), 1):
                        if re.search(pattern, line):
                            matches.append(f"{fp}:{i}:{line}")
                except Exception:
                    pass
        return {"matches": "\n".join(matches)}


async def tool_glob(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Find files matching a glob pattern."""
    pattern = arguments["pattern"]
    path = _resolve_path(arguments.get("path", "."), cwd)
    files = list(path.rglob(pattern))
    return {"files": [str(f.relative_to(path)) for f in files]}


async def tool_todo(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Manage a todo list (in-memory, per-session)."""
    # This is a simple in-memory store; real persistence is M1+
    return {"ok": True, "note": "todo is a no-op in M0; use agent's internal list"}


async def tool_exit_plan(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Placeholder: the agent loop intercepts exit_plan and asks the user."""
    return {"error": "exit_plan is only available in plan mode"}


# ── Registry builder ───────────────────────────────────────────────────


def build_registry() -> ToolRegistry:
    """Create the default tool registry with all M0 tools."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="read",
            description="Read a file with optional line range",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "default": 1},
                    "end": {"type": "integer"},
                },
                "required": ["path"],
            },
            side_effect=False,
        ),
        tool_read,
    )
    reg.register(
        ToolSpec(
            name="write",
            description="Write a file, creating parents",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            side_effect=True,
        ),
        tool_write,
    )
    reg.register(
        ToolSpec(
            name="edit",
            description="Edit a file by replacing old_string with new_string",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
            side_effect=True,
        ),
        tool_edit,
    )
    reg.register(
        ToolSpec(
            name="bash",
            description="Run a shell command",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "default": 30},
                    "cwd": {"type": "string"},
                },
                "required": ["command"],
            },
            side_effect=True,
        ),
        tool_bash,
    )
    reg.register(
        ToolSpec(
            name="grep",
            description="Search for a pattern in files",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "include": {"type": ["string", "array"]},
                    "exclude": {"type": ["string", "array"]},
                },
                "required": ["pattern"],
            },
            side_effect=False,
        ),
        tool_grep,
    )
    reg.register(
        ToolSpec(
            name="glob",
            description="Find files matching a glob pattern",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                },
                "required": ["pattern"],
            },
            side_effect=False,
        ),
        tool_glob,
    )
    reg.register(
        ToolSpec(
            name="todo",
            description="Manage a todo list",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action"],
            },
            side_effect=False,
        ),
        tool_todo,
    )
    reg.register(
        ToolSpec(
            name="exit_plan",
            description=(
                "Plan mode only: present your finished plan to the user for approval. "
                "On approval the session leaves plan mode and you may start implementing."
            ),
            parameters={
                "type": "object",
                "properties": {"plan": {"type": "string", "description": "The full plan, markdown"}},
                "required": ["plan"],
            },
            side_effect=False,
        ),
        tool_exit_plan,
    )
    return reg
