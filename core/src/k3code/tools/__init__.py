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
from k3code.reliability.sandbox import child_env, with_chdir
from k3code.tools.fuzzy_match import (
    format_no_match_hint,
    fuzzy_find_and_replace,
)
from k3code.tools.jobs import MAX_UNREAD_BYTES

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
    Symlinks are written through (the link itself is not replaced). A new file gets 0666 minus the umask, like any
    other program's (mkstemp made every new file 0600).
    """
    import secrets
    import stat

    target = path.resolve() if path.is_symlink() else path
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    for _ in range(100):
        tmp = str(target.parent / f".{target.name}.k3tmp-{secrets.token_hex(4)}")
        try:  # O_EXCL never follows or reuses an existing name; the kernel applies the umask to the new-file 0o666
            # An existing file's content is written under 0600 so a 0600 secret is never briefly world-readable;
            # the chmod to its real mode happens before the rename.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"no free temporary name next to {target}")
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


#: Files larger than this are only read as an explicit line range (streamed, never loaded whole).
MAX_READ_BYTES = 10 * 1024 * 1024


def _read_range(path: Path, start: int, end: int) -> dict[str, Any]:
    """Lines ``start..end`` of a large file, streamed, at most MAX_READ_BYTES of them."""
    selected: list[str] = []
    total = used = 0
    too_big = {"error": f"Lines {start}-{end} exceed {MAX_READ_BYTES // (1024 * 1024)} MB: request a smaller range"}
    with path.open("rb") as f:
        number = 1  # only \n ends a line here; a huge file is not re-split on \r
        while number <= end:
            raw = f.readline(MAX_READ_BYTES + 1)  # bounded: one endless line must not be loaded whole either
            if not raw:
                break
            if len(raw) > MAX_READ_BYTES and not raw.endswith(b"\n"):
                if number >= start:
                    return too_big
                while raw and not raw.endswith(b"\n"):  # skip the rest of a huge line before the range
                    raw = f.readline(1 << 20)
            elif number >= start:
                total += len(raw)
                if total > MAX_READ_BYTES:
                    return too_big
                line = _cap_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                used += len(line) + _NUMBER_WIDTH + 2
                if selected and used > READ_MAX_CHARS:
                    break  # the char budget is spent: the model continues with offset
                selected.append(line)
            number += 1
    if not selected:
        return {"content": "", "lines": f"{start}-{start} (past the end of the file)"}
    last = start + len(selected) - 1
    return {"content": "\n".join(selected), "lines": f"{start}-{last}", "first": start, "last": last, "total": None}


#: read: lines returned when no limit is given, chars kept of one line, and chars of numbered output per call. A read
#: has its own budget (it is not cut to MAX_TOOL_RESULT_CHARS): head+tail clipping lost the middle of a 400-line file
#: and the model read it again.
READ_DEFAULT_LIMIT = 2000
READ_MAX_LINE_CHARS = 2000
READ_MAX_CHARS = 60_000
_NUMBER_WIDTH = 6  # cat -n: the line number right-aligned in 6 columns, then a tab


def _read_window(arguments: dict[str, Any]) -> tuple[int, int | None]:
    """(first line, line count or None) from ``offset``/``limit``, or the older ``start``/``end`` (both 1-based)."""
    if "offset" in arguments or "limit" in arguments:
        first = int(arguments.get("offset") or 1)
        limit = arguments.get("limit")
        return max(1, first), (max(1, int(limit)) if limit is not None else None)
    first = max(1, int(arguments.get("start") or 1))
    end = arguments.get("end")
    return first, (max(1, int(end) - first + 1) if end is not None else None)


def _cap_line(line: str) -> str:
    if len(line) <= READ_MAX_LINE_CHARS:
        return line
    return f"{line[:READ_MAX_LINE_CHARS]}... [line cut: {len(line)} chars]"


async def tool_read(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Read a file: ``limit`` lines (default READ_DEFAULT_LIMIT) from line ``offset``, within READ_MAX_CHARS.

    ``start``/``end`` (an inclusive line range) still work. The result carries the raw lines (``content``) and where
    they are (``first``, ``last``, ``total``); format_tool_result numbers them for the model and says how to go on.
    """
    path = _resolve_path(arguments["path"], cwd)
    if not path.is_file():
        return {"error": f"File not found: {path}"}
    explicit = any(k in arguments for k in ("offset", "limit", "start", "end"))
    first, count = _read_window(arguments)
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        # read_bytes() of a multi-GB log took the shared daemon's memory with it: stream only the requested lines.
        if not explicit:
            return {
                "error": f"File is {size // (1024 * 1024)} MB (limit {MAX_READ_BYTES // (1024 * 1024)} MB): "
                "pass offset and limit (or start and end) to read a line range, or use grep to find what you need"
            }
        return _read_range(path, first, first + (count or READ_DEFAULT_LIMIT) - 1)
    text = path.read_bytes().decode("utf-8", errors="replace")  # reading may show U+FFFD; it never writes back
    lines = _split_lines(text)
    total = len(lines)
    first = min(first, total + 1)
    last_wanted = min(total, first - 1 + (count or READ_DEFAULT_LIMIT))
    selected: list[str] = []
    used = 0
    for line in lines[first - 1 : last_wanted]:
        line = _cap_line(line)
        cost = len(line) + _NUMBER_WIDTH + 2  # what the numbered line costs: number, tab, newline
        if selected and used + cost > READ_MAX_CHARS:
            break  # the char budget is spent: the model continues with offset
        selected.append(line)
        used += cost
    last = first + len(selected) - 1
    return {
        "content": "\n".join(selected),
        "lines": f"{first}-{max(first, last)} of {total}",
        "first": first,
        "last": last,
        "total": total,
    }


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
        return {
            "error": f"{path} is not valid UTF-8; refusing to edit it (that would destroy its bytes). "
            "Use bash (sed/iconv/python) for this file."
        }
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


#: bash: seconds before a foreground command is killed, when the call names no timeout, and the most it may name.
BASH_DEFAULT_TIMEOUT = 30
BASH_MAX_TIMEOUT = 600


async def _spawn_shell(
    cmd: str, workdir: Path, sandbox: list[str] | None, *, merge_stderr: bool = False
) -> asyncio.subprocess.Process:
    """``/bin/sh -c cmd`` in its own process group (so the whole tree can be killed), inside ``sandbox`` if given."""
    stderr = asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
    # start_new_session: its own process group, so the whole tree can be killed (setsid, without preexec_fn)
    if sandbox:
        return await asyncio.create_subprocess_exec(
            *sandbox,
            "/bin/sh",
            "-c",
            cmd,
            cwd=workdir,
            stdin=asyncio.subprocess.DEVNULL,  # the gateway's stdin is the TUI's JSON-RPC stream in stdio mode
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
            env=child_env(),  # the daemon's provider keys never reach a tool
        )
    return await asyncio.create_subprocess_shell(
        cmd,
        cwd=workdir,
        stdin=asyncio.subprocess.DEVNULL,  # the gateway's stdin is the TUI's JSON-RPC stream in stdio mode
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        start_new_session=True,
        env=child_env(),
    )


async def tool_bash(
    arguments: dict[str, Any], *, cwd: Path | None = None, sandbox: list[str] | None = None, session_id: str = ""
) -> dict[str, Any]:
    """Run a shell command with timeout and process-group kill.

    ``sandbox`` is a bwrap argv prefix (see ``reliability.sandbox``); the command then runs inside it.
    ``background: true`` starts it as a job of ``session_id`` (see ``k3code.tools.jobs``) and returns its job_id.
    """
    cmd = arguments["command"]
    timeout = min(float(arguments.get("timeout") or BASH_DEFAULT_TIMEOUT), BASH_MAX_TIMEOUT)
    workdir = _resolve_path(arguments.get("cwd", "."), cwd)
    proc: asyncio.subprocess.Process | None = None
    if sandbox:
        sandbox = with_chdir(sandbox, workdir)  # the command starts in the requested cwd, not the session's
    if arguments.get("background"):
        from k3code.tools import jobs

        try:
            job = jobs.REGISTRY.add(
                session_id, cmd, await _spawn_shell(cmd, workdir, sandbox, merge_stderr=True), cwd=str(workdir)
            )
        except Exception as e:
            return {"error": f"Failed to execute: {e}"}
        return {
            "content": f"started {job.id} (pid {job.proc.pid}) in the background: read its output with "
            f'bash_output {{"job_id": "{job.id}"}}, stop it with bash_kill'
        }
    from k3code.tools import jobs as _jobs

    try:
        proc = await _spawn_shell(cmd, workdir, sandbox)
        _jobs.REGISTRY.track(session_id, cmd, proc.pid, str(workdir))  # the TUI's process dock (process.list)
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
                "error": f"Command timed out after {timeout:g}s; retry with timeout up to {BASH_MAX_TIMEOUT} "
                "or background: true",
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
        if proc is not None:
            _jobs.REGISTRY.untrack(proc.pid)
            if proc.returncode is None:
                await asyncio.shield(_kill_group(proc))


def format_tool_result(result: dict[str, Any]) -> Any:
    """What the model, the journal and the TUI see of one tool result.

    ``bash``, ``grep`` and ``glob`` carry free text; they used to be sent as the Python repr of their dict, which
    escapes every newline, quote and backslash: ``{'stdout': 'a\\nb\\n', 'stderr': '', 'exit_code': 0}``. The TUI
    showed that as one unreadable line, and a model that copied a line it had read that way into an ``edit`` copied
    the escapes too. They are plain text now (stdout as is; stderr, an error note and a non-zero exit code only when
    there is something to say). ``read`` is numbered like ``cat -n`` and ends with how to read on when it stopped
    before the end of the file. Every other result is unchanged: other ``content``, small ok/error dicts.
    """
    if "content" in result and "first" in result:  # read
        return _numbered_read(result)
    if "content" in result:
        return result["content"]
    if "stdout" in result or "stderr" in result:  # bash
        out = str(result.get("stdout") or "").rstrip("\n")
        err = str(result.get("stderr") or "").rstrip("\n")
        code = result.get("exit_code")
        parts = [out] if out else []
        if err:
            parts.append(f"[stderr]\n{err}")
        if result.get("error"):  # timeout, output limit
            parts.append(f"[{result['error']}]")
        if code not in (0, None):
            parts.append(f"[exit code {code}]")
        return "\n".join(parts) if parts else "(no output)"
    if "matches" in result:  # grep
        text = str(result.get("matches") or "")
        if result.get("warnings"):
            text = f"{text}\n[{result['warnings']}]" if text else f"[{result['warnings']}]"
        return text or "(no matches)"
    if "files" in result and isinstance(result["files"], list):  # glob
        text = "\n".join(str(f) for f in result["files"]) or "(no files)"
        return f"{text}\n[{result['note']}]" if result.get("note") else text
    if result.keys() == {"ok", "path"} and result["ok"] is True:  # write
        return f"Wrote {result['path']}"
    if result.keys() == {"ok", "replacements", "strategy"} and result["ok"] is True:  # edit
        n = result["replacements"]
        how = "" if result["strategy"] == "exact" else f" (matched by {result['strategy']}, not exactly)"
        return f"Edited: {n} replacement{'' if n == 1 else 's'}{how}"
    if result.keys() == {"error"}:
        return f"Error: {result['error']}"
    return str(result)


def _numbered_read(result: dict[str, Any]) -> str:
    first, last, total = int(result["first"]), int(result["last"]), result.get("total")
    if last < first:
        if total == 0:
            return "(empty file)"
        return f"[file has {total} lines; offset {first} is past the end]"
    lines = str(result["content"]).split("\n")
    body = "\n".join(f"{n:>{_NUMBER_WIDTH}}\t{line}" for n, line in enumerate(lines, first))
    if total is None:  # a huge file, streamed: its length is not known
        return f"{body}\n[showed {first}-{last}; continue with offset={last + 1}]"
    if last < total:
        return f"{body}\n[file has {total} lines; showed {first}-{last}; continue with offset={last + 1}]"
    return body


#: Per stream: bytes after which the command is killed.
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
#: What a command's output keeps (the first and the last bytes); the transcript stores exactly this.
_KEEP_HEAD_BYTES = 40_000
_KEEP_TAIL_BYTES = 10_000
#: The most of one tool result the model is sent, in chars (see clip_tool_results). The bash cap as before.
MAX_TOOL_RESULT_CHARS = 10_000  # the default for what the model is sent of one tool result (context.tool_output_chars)
_MAX_CHARS = MAX_TOOL_RESULT_CHARS


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


#: How to get at a clipped middle, per tool (the clip marker says it); other tools get _CLIP_HINT_DEFAULT.
_CLIP_HINTS = {
    "bash": "rerun piped through grep, head, tail or sed -n",
    "grep": "grep a narrower pattern or path",
    "glob": "glob a narrower pattern or path",
    "read": "read with offset and limit",
}
_CLIP_HINT_DEFAULT = "narrow the request to see the rest"
#: A read result has its own budget (READ_MAX_CHARS); this is only a backstop for read results stored before it.
_READ_RESULT_CHARS = READ_MAX_CHARS + 1000


def clip_head_tail(text: str, limit: int = _MAX_CHARS, hint: str = _CLIP_HINT_DEFAULT) -> str:
    """``text`` cut to ``limit`` chars: the first 3/5 and the last 2/5 are kept, the middle becomes a marker that
    states how many chars it dropped and (``hint``) how to see them. Text within the limit is returned unchanged."""
    if len(text) <= limit:
        return text
    head = limit * 3 // 5
    tail = limit - head
    dropped = len(text) - head - tail
    return (
        f"{text[:head]}\n... [truncated {dropped} chars from the middle of {len(text)}; "
        f"first {head} and last {tail} shown; {hint}] ...\n{text[-tail:]}"
    )


def clip_for_model(name: str | None, text: str, limit: int = _MAX_CHARS) -> str:
    """What the model is sent of one tool result: ``read`` keeps its own budget, others are clipped to ``limit``."""
    if name == "read":
        limit = max(limit, _READ_RESULT_CHARS)
    return clip_head_tail(text, limit, _CLIP_HINTS.get(name or "", _CLIP_HINT_DEFAULT))


def clip_tool_results(messages: Sequence[Message], limit: int = _MAX_CHARS) -> list[Message]:
    """The messages a provider receives: every tool result over its limit becomes its head+tail clip (clip_for_model).

    Pure per message, so one history always yields the same bytes for the same prefix (prompt caches can hit). The
    input list and its messages are untouched: the transcript and the session keep the full results.
    """
    out: list[Message] = []
    for m in messages:
        if m.role == "tool" and m.content:
            clipped = clip_for_model(m.name, m.content, limit)
            if clipped is not m.content:
                m = dataclasses.replace(m, content=clipped)
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
            for inc in include if isinstance(include, list) else [include]:
                cmd += ["-g", inc]
        if exclude:
            for exc in exclude if isinstance(exclude, list) else [exclude]:
                cmd += ["-g", f"!{exc}"]
        # -e and -- keep a pattern such as "--files" a literal search term, never an rg option
        cmd += ["-e", pattern, "--", str(path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
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
        # Python fallback (no rg): off the event loop, same tree rules as glob, capped
        return await asyncio.to_thread(_grep_fallback, pattern, path, include, exclude)


#: Directories glob and the grep fallback never enter (rg skips them through .gitignore or as hidden).
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "dist"})
#: Most paths a glob returns, and most lines the grep fallback returns; past it the result says it was cut.
MAX_SEARCH_RESULTS = 1000


def _git_files(path: Path) -> list[str] | None:
    """Files under ``path`` that git tracks or would track (``.gitignore`` honoured), relative to ``path``; None when
    ``path`` is not in a git work tree or git is missing."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(path), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=15,
            check=False,
            env=child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # -c lists index entries whose file is gone (deleted or moved without staging): the work tree decides.
    return [f for f in out.stdout.decode("utf-8", errors="replace").split("\0") if f and os.path.lexists(path / f)]


def _tree(path: Path) -> tuple[list[str], list[str]]:
    """(files, directories) under ``path`` as relative posix paths, without SKIP_DIRS: from git when ``path`` is in a
    work tree (so ``.gitignore`` holds), else from a walk that prunes them."""
    tracked = _git_files(path)
    if tracked:  # empty: not a work tree, or a path git ignores as a whole (asked for on purpose): walk it
        files = [f for f in tracked if not SKIP_DIRS.intersection(f.split("/")[:-1])]
        dirs = sorted({"/".join(f.split("/")[:i]) for f in files for i in range(1, f.count("/") + 1)})
        return files, dirs
    files, dirs = [], []
    for root, subdirs, names in os.walk(path):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(path).as_posix()
        prefix = "" if rel == "." else rel + "/"
        dirs.extend(prefix + d for d in subdirs)
        files.extend(prefix + n for n in names)
    return files, dirs


def _match_parts(parts: list[str], pats: list[str]) -> bool:
    import fnmatch

    if not pats:
        return not parts
    if pats[0] == "**":
        return any(_match_parts(parts[i:], pats[1:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pats[0]) and _match_parts(parts[1:], pats[1:])


def _glob_match(rel: str, pattern: str) -> bool:
    """``Path.rglob`` semantics: ``pattern`` may start at any depth (``*.py`` finds ``a/b/c.py``)."""
    pats = [p for p in pattern.split("/") if p not in ("", ".")]
    return _match_parts(rel.split("/"), ["**", *pats])


def _capped(items: list[str]) -> tuple[list[str], str]:
    if len(items) <= MAX_SEARCH_RESULTS:
        return items, ""
    note = f"truncated to the first {MAX_SEARCH_RESULTS} results: narrow the pattern or path"
    return items[:MAX_SEARCH_RESULTS], note


def _glob_sync(path: Path, pattern: str) -> dict[str, Any]:
    files, dirs = _tree(path)
    found, note = _capped(sorted(p for p in (*files, *dirs) if _glob_match(p, pattern)))
    return {"files": found, **({"note": note} if note else {})}


def _grep_fallback(pattern: str, path: Path, include: Any, exclude: Any) -> dict[str, Any]:
    import re

    includes = include if isinstance(include, list) else [include] if include else []
    excludes = exclude if isinstance(exclude, list) else [exclude] if exclude else []
    rx = re.compile(pattern)
    candidates = [path] if path.is_file() else [path / f for f in _tree(path)[0]]
    matches: list[str] = []
    for fp in candidates:
        if includes and not any(Path(fp.name).match(p) for p in includes):
            continue
        if excludes and any(Path(fp.name).match(p) for p in excludes):
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches.extend(f"{fp}:{i}:{line}" for i, line in enumerate(text.splitlines(), 1) if rx.search(line))
        if len(matches) > MAX_SEARCH_RESULTS:
            break
    shown, note = _capped(matches)
    return {"matches": "\n".join(shown), **({"warnings": note} if note else {})}


async def tool_glob(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Find files matching a glob pattern (skipping .git, node_modules, .venv, dist and what .gitignore ignores)."""
    pattern = arguments["pattern"]
    path = _resolve_path(arguments.get("path", "."), cwd)
    return await asyncio.to_thread(_glob_sync, path, pattern)


async def tool_todo(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Manage a todo list (in-memory, per-session)."""
    # This is a simple in-memory store; real persistence is M1+
    return {"ok": True, "note": "todo is a no-op in M0; use agent's internal list"}


async def tool_bash_output(
    arguments: dict[str, Any], *, cwd: Path | None = None, session_id: str = ""
) -> dict[str, Any]:
    """New output of a background job since the last read, and whether it still runs."""
    from k3code.tools import jobs

    return await jobs.REGISTRY.read(session_id, str(arguments["job_id"]))


async def tool_bash_kill(arguments: dict[str, Any], *, cwd: Path | None = None, session_id: str = "") -> dict[str, Any]:
    """Stop a background job of this session (its whole process group)."""
    from k3code.tools import jobs

    return await jobs.REGISTRY.kill(session_id, str(arguments["job_id"]))


#: Tools whose handler takes the session id (background jobs belong to the session that started them).
SESSION_TOOLS = frozenset({"bash", "bash_output", "bash_kill"})


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
            description=(
                "Read a text file. Lines come numbered (number, tab, line); the numbers are not part of the file. "
                f"Returns up to {READ_DEFAULT_LIMIT} lines; a longer file ends with a note saying which offset "
                "continues it.\n"
                f"Limits: {READ_DEFAULT_LIMIT} lines and {READ_MAX_CHARS} chars per call, lines cut at "
                f"{READ_MAX_LINE_CHARS} chars; a file over {MAX_READ_BYTES // (1024 * 1024)} MB needs offset+limit."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "First line to read (1-based)", "default": 1},
                    "limit": {
                        "type": "integer",
                        "description": f"How many lines to read (default {READ_DEFAULT_LIMIT})",
                    },
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
            description="Write a file, creating parents.\n"
            "Limits: replaces the whole file atomically (an existing file keeps its mode); UTF-8 text only.",
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
            description="Edit a file by replacing old_string with new_string.\n"
            "Limits: old_string must match exactly once unless replace_all (whitespace-tolerant matching is a "
            "fallback); UTF-8 files only; line endings are kept.",
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
            description="Run a shell command (/bin/sh -c) and return its stdout, stderr and exit code.\n"
            f"Limits: killed after {BASH_DEFAULT_TIMEOUT} s by default (timeout: up to {BASH_MAX_TIMEOUT} s) or "
            f"past {MAX_OUTPUT_BYTES // (1024 * 1024)} MB of output; you see at most {MAX_TOOL_RESULT_CHARS} chars "
            "(head and tail). For servers, watchers and long builds use background: true, then bash_output.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {
                        "type": "number",
                        "description": f"Seconds before the command is killed (default {BASH_DEFAULT_TIMEOUT}, max "
                        f"{BASH_MAX_TIMEOUT}); ignored with background",
                        "default": BASH_DEFAULT_TIMEOUT,
                    },
                    "cwd": {"type": "string", "description": "Directory to run in, relative to the session cwd"},
                    "background": {
                        "type": "boolean",
                        "description": "Start the command and return a job_id at once, with no timeout; read its "
                        "output with bash_output, stop it with bash_kill. Jobs end with the session.",
                        "default": False,
                    },
                },
                "required": ["command"],
            },
            side_effect=True,
        ),
        tool_bash,
    )
    reg.register(
        ToolSpec(
            name="bash_output",
            description="Output a background bash job printed since the last read, and whether it still runs "
            "(or its exit code).\n"
            f"Limits: keeps the newest {MAX_UNREAD_BYTES // 1024} KB of unread output per job; you see at most "
            f"{MAX_TOOL_RESULT_CHARS} chars (head and tail).",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "From bash with background: true"}},
                "required": ["job_id"],
            },
            side_effect=False,
        ),
        tool_bash_output,
    )
    reg.register(
        ToolSpec(
            name="bash_kill",
            description="Stop a background bash job (its whole process group).\n"
            "Limits: only jobs this session started.",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            side_effect=True,
        ),
        tool_bash_kill,
    )
    reg.register(
        ToolSpec(
            name="grep",
            description="Search for a regex in files (ripgrep; .gitignore is respected). Returns path:line:text.\n"
            f"Limits: stops after 10 s; you see at most {MAX_TOOL_RESULT_CHARS} chars (head and tail).",
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
            description="Find files matching a glob pattern (recursive under path).\n"
            f"Limits: paths relative to path; you see at most {MAX_TOOL_RESULT_CHARS} chars (head and tail).",
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
            description="Manage a todo list.\nLimits: a no-op placeholder; nothing is stored, keep the list yourself.",
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
