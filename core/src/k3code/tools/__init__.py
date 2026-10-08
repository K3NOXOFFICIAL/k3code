"""Tool registry and implementations."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from k3code.providers.types import Message, ToolSpec
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


def _split_lines(text: str) -> list[str]:
    """Lines as an editor numbers them: only \\n, \\r\\n and \\r end a line (str.splitlines() also splits on \\x0c,
    \\x1c-\\x1e, \\x85 and U+2028/9, so reported line numbers disagreed with every editor and with the matcher)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically (temp file in the same directory + rename), keeping its mode.

    A plain write_text() truncates first: a kill -9 or a full disk in the middle left a half-written source file.
    Symlinks are written through (the link itself is not replaced).
    """
    import stat
    import tempfile

    target = path.resolve() if path.is_symlink() else path
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.k3tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def tool_read(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Read a file, optionally with line ranges."""
    path = _resolve_path(arguments["path"], cwd)
    if not path.is_file():
        return {"error": f"File not found: {path}"}
    start = arguments.get("start", 1)
    end = arguments.get("end")
    text = path.read_bytes().decode("utf-8", errors="replace")  # reading may show U+FFFD; it never writes back
    lines = _split_lines(text)
    if end is None:
        end = len(lines)
    start = max(1, min(start, len(lines) + 1))
    end = max(start, min(end, len(lines)))
    selected = lines[start - 1 : end]
    return {"content": "\n".join(selected), "lines": f"{start}-{end} of {len(lines)}"}


async def tool_write(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Write a file, creating parent directories (atomically; an existing file keeps its mode)."""
    path = _resolve_path(arguments["path"], cwd)
    content = arguments["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, content.encode("utf-8"))
    return {"ok": True, "path": str(path)}


async def tool_edit(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Edit a file using fuzzy find-and-replace, preserving its bytes outside the edit (line endings included)."""
    path = _resolve_path(arguments["path"], cwd)
    if not path.is_file():
        return {"error": f"File not found: {path}"}
    old_string = arguments["old_string"]
    new_string = arguments["new_string"]
    replace_all = arguments.get("replace_all", False)
    raw = path.read_bytes()
    try:
        content = raw.decode("utf-8")  # strict: errors="replace" wrote U+FFFD over latin-1/binary bytes for good
    except UnicodeDecodeError:
        return {"error": f"{path} is not valid UTF-8; refusing to edit it (that would destroy its bytes). "
                         "Use bash (sed/iconv/python) for this file."}
    # Match against LF text, write back with the file's own line endings (a pure-CRLF file stays CRLF).
    crlf = "\r\n" in content and content.count("\r\n") == content.count("\n")
    crlf = crlf and "\r" not in content.replace("\r\n", "")  # no lone CR either: every line end is CRLF
    if crlf:
        content = content.replace("\r\n", "\n")
        old_string, new_string = old_string.replace("\r\n", "\n"), new_string.replace("\r\n", "\n")
    new_content, count, strategy, error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all=replace_all
    )
    if error:
        hint = format_no_match_hint(error, count, old_string, content)
        return {"error": error + hint}
    if crlf:
        new_content = new_content.replace("\n", "\r\n")
    _atomic_write(path, new_content.encode("utf-8"))
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
    proc: asyncio.subprocess.Process | None = None
    try:
        # start_new_session: its own process group, so the whole tree can be killed (setsid, without preexec_fn)
        if sandbox:
            proc = await asyncio.create_subprocess_exec(
                *sandbox,
                "/bin/sh",
                "-c",
                cmd,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        out, err = _Capture(), _Capture()
        # Streams are read incrementally into bounded buffers: communicate() held every byte in the shared daemon's
        # memory (a runaway `yes` or `cat huge.log` took it to hundreds of MB in a second and the OOM killer then took
        # every session with it). Past MAX_OUTPUT_BYTES the command is killed.
        readers = [
            asyncio.ensure_future(_drain(proc.stdout, out, proc)),
            asyncio.ensure_future(_drain(proc.stderr, err, proc)),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*readers, proc.wait()), timeout=timeout)
        except TimeoutError:
            await _kill_group(proc)
            return {
                "error": f"Command timed out after {timeout}s",
                "stdout": out.text(),
                "stderr": err.text(),
                "exit_code": -1,
            }
        if out.overflowed or err.overflowed:
            return {
                "error": f"Output limit exceeded ({MAX_OUTPUT_BYTES // (1024 * 1024)} MB): command killed",
                "stdout": out.text(),
                "stderr": err.text(),
                "exit_code": -9,
            }
        return {"stdout": out.text(), "stderr": err.text(), "exit_code": proc.returncode}
    except Exception as e:
        return {"error": f"Failed to execute: {e}"}
    finally:
        # Every exit path, including /stop (CancelledError) and a daemon shutdown, must not leave the tree running.
        if proc is not None and proc.returncode is None:
            await asyncio.shield(_kill_group(proc))


#: Per stream: bytes after which the command is killed.
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
#: What a command's output keeps (the first and the last bytes); the transcript stores exactly this.
_KEEP_HEAD_BYTES = 40_000
_KEEP_TAIL_BYTES = 10_000
#: The most of one tool result the model is sent, in chars (see clip_tool_results). The bash cap as before.
_MAX_CHARS = 10_000


class _Capture:
    """Bounded capture of one output stream: the first and the last bytes, a byte count, an overflow flag."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.overflowed = False

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = _KEEP_HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > _KEEP_TAIL_BYTES:
                del self.tail[: len(self.tail) - _KEEP_TAIL_BYTES]

    def text(self) -> str:
        head, tail = bytes(self.head), bytes(self.tail)
        dropped = self.total - len(head) - len(tail)  # bytes the capture itself could not keep
        if dropped <= 0:
            return (head + tail).decode("utf-8", errors="replace")
        return (
            head.decode("utf-8", errors="replace")
            + f"\n... [truncated {dropped} bytes not kept by the capture] ...\n"
            + tail.decode("utf-8", errors="replace")
        )


def clip_head_tail(text: str, limit: int = _MAX_CHARS) -> str:
    """``text`` cut to ``limit`` chars: the first 3/5 and the last 2/5 are kept, the middle becomes a marker that
    states how many chars it dropped. Text within the limit is returned unchanged."""
    if len(text) <= limit:
        return text
    head = limit * 3 // 5
    tail = limit - head
    dropped = len(text) - head - tail
    return (
        f"{text[:head]}\n... [truncated {dropped} chars from the middle of {len(text)}; "
        f"first {head} and last {tail} shown] ...\n{text[-tail:]}"
    )


def clip_tool_results(messages: Sequence[Message], limit: int = _MAX_CHARS) -> list[Message]:
    """The messages a provider receives: every tool result longer than ``limit`` becomes its head+tail clip.

    Pure per message, so one history always yields the same bytes for the same prefix (prompt caches can hit). The
    input list and its messages are untouched: the transcript and the session keep the full results.
    """
    out: list[Message] = []
    for m in messages:
        if m.role == "tool" and m.content and len(m.content) > limit:
            m = dataclasses.replace(m, content=clip_head_tail(m.content, limit))
        out.append(m)
    return out


async def _drain(stream: asyncio.StreamReader | None, cap: _Capture, proc: asyncio.subprocess.Process) -> None:
    """Read ``stream`` to EOF into ``cap``; kill the command's group when it floods past MAX_OUTPUT_BYTES."""
    if stream is None:
        return
    while chunk := await stream.read(64 * 1024):
        cap.feed(chunk)
        if cap.total > MAX_OUTPUT_BYTES:
            cap.overflowed = True
            await _kill_group(proc)
            return


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM then SIGKILL the command's whole process group and reap it; never raises."""
    import contextlib
    import signal

    pgid = proc.pid  # start_new_session made the command a group leader
    for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except TimeoutError:
            continue
        break
    # SIGTERM may have stopped the leader while a child ignoring it keeps running in the group
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


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
        # -e and -- keep a pattern such as "--files" a literal search term, never an rg option
        cmd += ["-e", pattern, "--", str(path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()  # rg kept scanning after the timeout
            await proc.wait()
            return {"error": "grep timed out after 10 s: narrow the path or the pattern"}
        if proc.returncode == 2 and stdout.strip():
            # rg exits 2 when any entry was unreadable (a root-owned dir) but still prints every other match
            note = stderr.decode("utf-8", errors="replace").strip().splitlines()[:3]
            return {"matches": stdout.decode("utf-8", errors="replace").strip(), "warnings": note}
        if proc.returncode not in (0, 1):  # 1 = no matches
            raise RuntimeError(stderr.decode("utf-8", errors="replace"))
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
                "Plan mode only: submit the finished plan for user approval; on approval you leave plan mode "
                "and may implement it."
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
