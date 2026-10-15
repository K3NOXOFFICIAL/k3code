"""Tool registry and implementations."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from k3code.permissions.engine import search_withheld, sensitive_path
from k3code.providers.types import Message, ToolSpec
from k3code.reliability.sandbox import child_env, with_chdir
from k3code.tools.fuzzy_match import (
    SIMILARITY_STRATEGIES,
    STRATEGIES,
    format_no_match_hint,
    fuzzy_find_and_replace,
)
from k3code.tools.jobs import MAX_UNREAD_BYTES

# ── Tool registry ──────────────────────────────────────────────────────


class ToolRegistry:
    """Registry of available tools with JSON-schema specs.

    :meth:`specs` is the tools array of every model request, and providers cache the request from its first byte (the
    system prompt, then the tools array): it must only ever grow at its end. So the always-advertised tools come first,
    in registration order, and deferred tools (MCP) that ``mcp_tool_search`` activated follow, in activation order. An
    activated tool listed at its registration position landed mid-array (after ``skill``, before ``mcp_tool_search``
    and the task and web tools) and invalidated the cached prefix of every later request (#168).

    The activation order lives in ``state`` (see :meth:`keep_activated_in`): the session's persisted meta in the
    gateway, so the next turn's loop, or a higher tier's after an escalation, starts with the same tools in the same
    order instead of losing them (and the cache) and searching again.
    """

    #: The key of ``state`` that holds the activated deferred tools' names, in activation order.
    ACTIVATED_KEY = "mcp_activated"

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, callable]] = {}
        self._deferred: set[str] = set()  # registered + callable, but schema hidden until activated
        self._state: dict[str, Any] = {}  # where the activation order lives (see keep_activated_in)

    def register(self, spec: ToolSpec, handler: callable, *, deferred: bool = False) -> None:
        self._tools[spec.name] = (spec, handler)
        if deferred:
            self._deferred.add(spec.name)
        else:
            self._deferred.discard(spec.name)

    def keep_activated_in(self, state: dict[str, Any]) -> None:
        """Keep the activation order in ``state`` (the session's meta), from now on and including what was activated
        here before: every registry built on the same ``state`` advertises the same activated tools, in one order."""
        earlier = self.activated()
        self._state = state
        self.activate(earlier)

    def activated(self) -> list[str]:
        """Names activated so far, in activation order (a malformed stored value counts as none)."""
        raw = self._state.get(self.ACTIVATED_KEY)
        return [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []

    def activate(self, names: list[str]) -> None:
        """Start advertising the schemas of deferred tools (``mcp_tool_search``): appended after every tool already
        advertised, so the tools array of the requests before stays its prefix."""
        order = self.activated()
        new = [n for n in dict.fromkeys(names) if n in self._deferred and n not in order]
        if new:
            self._state[self.ACTIVATED_KEY] = [*order, *new]

    def get(self, name: str) -> tuple[ToolSpec, callable] | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        """Always-advertised tools in registration order, then the activated deferred ones in activation order (a
        name activated in an earlier loop whose MCP server has gone since is skipped)."""
        plain = [spec for n, (spec, _) in self._tools.items() if n not in self._deferred]
        return plain + [self._tools[n][0] for n in self.activated() if n in self._deferred and n in self._tools]

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


#: Errors of the temp-file path that say "this directory or mount cannot do a rename here", not "the disk is broken":
#: a read-only directory (EACCES/EPERM), a file held open by another OS (WSL on /mnt/c) or bind-mounted (EBUSY), a
#: rename across mounts (EXDEV). ENOSPC, EIO and EROFS are not in it: rewriting in place would not help.
_IN_PLACE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY, errno.EXDEV})


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically (temp file in the same directory + rename), keeping its mode.

    A plain write_text() truncates first: a kill -9 or a full disk in the middle left a half-written source file.
    Symlinks are written through (the link itself is not replaced). A new file gets 0666 minus the umask, like any
    other program's (mkstemp made every new file 0600). Run as root, the new file keeps the old file's owner.

    When the temp-file path is refused (see ``_IN_PLACE_ERRNOS``) and the file exists, it is rewritten in place
    instead: not crash-safe, but the only way into a writable file in a read-only directory or on a bind mount.
    """
    target = path.resolve() if path.is_symlink() else path
    old = target.stat() if target.exists() else None
    try:
        _write_via_temp(target, data, old)
    except OSError as exc:
        if old is None or exc.errno not in _IN_PLACE_ERRNOS:
            raise
        _write_in_place(target, data, exc)


def _write_via_temp(target: Path, data: bytes, old: os.stat_result | None) -> None:
    import secrets
    import stat

    mode = stat.S_IMODE(old.st_mode) if old is not None else None
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
        if old is not None:
            os.chmod(tmp, mode)
            if getattr(os, "geteuid", lambda: -1)() == 0:
                with contextlib.suppress(OSError):
                    os.chown(tmp, old.st_uid, old.st_gid)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_in_place(target: Path, data: bytes, refused: OSError) -> None:
    """Truncate-and-write ``target`` itself. The old bytes are kept in memory and put back if the write fails."""
    try:
        before = target.read_bytes()
    except OSError:
        raise refused from None  # the original error is the useful one
    try:
        _overwrite(target, data)
    except BaseException:
        with contextlib.suppress(OSError):
            _overwrite(target, before)
        raise


def _overwrite(target: Path, data: bytes) -> None:
    with open(target, "r+b") as f:
        f.write(data)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


#: The tools FileStamps guards: they change a file, so the model must have seen what is in it now.
FILE_TOOLS = frozenset({"write", "edit"})


class FileStamps:
    """What a session last saw of each file it read, wrote or edited: real path → (mtime_ns, size).

    ``write`` and ``edit`` refuse an existing file the session has not seen, or one that changed on disk since (the
    user's editor, a formatter, another agent): write replaced the whole file and that change was gone without a word.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[int, int]] = {}

    def record(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            st = path.stat()
            self._seen[os.path.realpath(path)] = (st.st_mtime_ns, st.st_size)

    def refusal(self, tool: str, path: Path) -> str | None:
        """Why ``tool`` may not change ``path`` now, or None. A file that does not exist yet is always fine."""
        try:
            st = path.stat()
        except OSError:
            return None
        if not path.is_file():
            return None  # the tool reports what is wrong with it
        seen = self._seen.get(os.path.realpath(path))
        if seen is None:
            return f"{path} exists and you have not read it: read it before you {tool} it"
        if seen != (st.st_mtime_ns, st.st_size):
            return (
                f"{path} changed since you read it (by the user or another program): read it again before you {tool} it"
            )
        return None


#: Files larger than this are only read as an explicit line range (streamed, never loaded whole).
MAX_READ_BYTES = 10 * 1024 * 1024


def _file_size_note(size: int) -> str:
    return f"File is {size // (1024 * 1024)} MB (limit {MAX_READ_BYTES // (1024 * 1024)} MB)"


def _too_big_to_read(path: Path) -> dict[str, Any] | None:
    """edit loads the whole file and matches it in memory, so it takes no more than read would load whole."""
    size = path.stat().st_size
    if size <= MAX_READ_BYTES:
        return None
    return {"error": f"{_file_size_note(size)}: edit cannot load it; change it with bash (sed, python) instead"}


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
    Runs on a worker thread: the gateway serves every session from one event loop (see tool_edit).
    """
    return await asyncio.to_thread(_read_sync, arguments, cwd)


def _read_sync(arguments: dict[str, Any], cwd: Path | None) -> dict[str, Any]:
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
                "error": f"{_file_size_note(size)}: "
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
    return await asyncio.to_thread(_write_sync, arguments, cwd)


def _write_sync(arguments: dict[str, Any], cwd: Path | None) -> dict[str, Any]:
    path = _resolve_path(arguments["path"], cwd)
    content = arguments["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, content.encode("utf-8"))
    return {"ok": True, "path": str(path)}


@dataclasses.dataclass
class EditPlan:
    """What ``edit`` will write: the file's text before and after, both with LF line ends."""

    path: Path
    before: str
    after: str
    crlf: bool  # written back with CRLF line ends
    count: int
    strategy: str
    replaced: str | None = None  # a similarity match only: the file's own text that old_string stood for
    line: int = 0  # the first line of ``replaced``


def plan_edit(arguments: dict[str, Any], *, cwd: Path | None = None) -> EditPlan | dict[str, Any]:
    """The change ``edit`` would make, or its error result; nothing is written. The approval prompt shows the same
    change (issue #131), so this is the one place the replacement is computed."""
    path = _resolve_path(arguments["path"], cwd)
    if not path.is_file():
        return {"error": f"File not found: {path}"}
    old_string = arguments["old_string"]
    new_string = arguments["new_string"]
    replace_all = arguments.get("replace_all", False)
    if (too_big := _too_big_to_read(path)) is not None:
        return too_big
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
    replaced, line = None, 0
    if strategy in SIMILARITY_STRATEGIES:
        # old_string only resembles what is replaced (a value the model misremembered): name the file's real text.
        # Such a match is always unique, so the strategy finds the same one span again.
        [(start, end)] = dict(STRATEGIES)[strategy](content, old_string)
        replaced, line = content[start:end], content.count("\n", 0, start) + 1
    return EditPlan(path, content, new_content, crlf, count, strategy, replaced, line)


async def tool_edit(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Edit a file using fuzzy find-and-replace, preserving its bytes outside the edit (line endings included).

    The body runs on a worker thread: the gateway serves every session from one event loop, and a failed edit on a
    big file tries every match strategy and then a similarity search over each line.
    """
    return await asyncio.to_thread(_edit_sync, arguments, cwd)


def _edit_sync(arguments: dict[str, Any], cwd: Path | None) -> dict[str, Any]:
    plan = plan_edit(arguments, cwd=cwd)
    if isinstance(plan, dict):
        return plan
    new_content = plan.after.replace("\n", "\r\n") if plan.crlf else plan.after
    _atomic_write(plan.path, new_content.encode("utf-8"))
    result = {"ok": True, "replacements": plan.count, "strategy": plan.strategy}
    if plan.replaced is not None:
        result.update(replaced=plan.replaced, line=plan.line)
    return result


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


#: What is stored of one tool result (transcript, messages.json, sessions.db); the model is sent far less
#: (MAX_TOOL_RESULT_CHARS), this only stops one runaway result from bloating every checkpoint.
MAX_STORED_RESULT_CHARS = 1_000_000


def format_tool_result(result: dict[str, Any]) -> Any:
    """``_format_tool_result`` with a string result capped at ``MAX_STORED_RESULT_CHARS`` (head + tail)."""
    out = _format_tool_result(result)
    if isinstance(out, str) and len(out) > MAX_STORED_RESULT_CHARS:
        return clip_head_tail(out, MAX_STORED_RESULT_CHARS)
    return out


def _format_tool_result(result: dict[str, Any]) -> Any:
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
    if result.keys() - {"replaced", "line"} == {"ok", "replacements", "strategy"} and result["ok"] is True:  # edit
        n = result["replacements"]
        how = "" if result["strategy"] == "exact" else f" (matched by {result['strategy']}, not exactly)"
        text = f"Edited: {n} replacement{'' if n == 1 else 's'}{how}"
        if "replaced" in result:  # a similarity match: the text it replaced is not old_string
            first = result["line"]
            last = first + result["replaced"].count("\n")
            where = f"line {first}" if first == last else f"lines {first}-{last}"
            text += (
                f"\nold_string is not in the file; it replaced this similar text at {where}. "
                f"Check it is what you meant:\n{result['replaced']}"
            )
        return text
    if result.keys() == {"error"}:
        return f"Error: {result['error']}"
    return json.dumps(result, ensure_ascii=False, default=str)


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


#: rg excludes on top of its own defaults (.gitignore, hidden files): dependency trees and other sessions' worktrees.
_RG_EXCLUDES = ("!node_modules/", "!__pycache__/", "!.venv/", "!**/.claude/worktrees/", "!**/.claude/jobs/")
#: Longest line rg prints in full; minified bundles and data blobs would otherwise fill the context with one match.
_RG_MAX_COLUMNS = 400


def _grep_line(raw: bytes, verdicts: dict[bytes, bool], withheld: Callable[[str], bool]) -> tuple[str, bool]:
    """An ``rg --null`` output line as ``path:line:text``, and whether it comes from a credential file (``withheld``,
    see ``search_withheld``: dotenv files, ``~/.ssh``, the credential stores and, searching ``$HOME``, the dot folders
    there). An include glob such as ``*`` or ``.env*`` turns rg's hidden-file filter off, so this is what keeps
    ``.env`` out of a search.

    The file name stays bytes until the check: decoded with replacement characters first, a path with bytes that
    are not UTF-8 (a home directory named so) would no longer match ``~/.ssh`` and the like. ``verdicts`` caches
    the answer per file name (the check resolves symlinks, too slow to repeat for every matching line)."""
    name, sep, rest = raw.partition(b"\0")
    if not sep:  # one file searched: rg prints no name, and tool_grep has decided on that path already
        return raw.decode("utf-8", errors="replace"), False
    if (hide := verdicts.get(name)) is None:
        hide = verdicts[name] = withheld(os.path.abspath(os.fsdecode(name)))
    return f"{name.decode('utf-8', errors='replace')}:{rest.decode('utf-8', errors='replace')}", hide


def _note_withheld(result: dict[str, Any], note: str) -> dict[str, Any]:
    """``result`` with ``note`` added to its warnings (a string, or a list of stderr lines)."""
    warnings = result.get("warnings")
    if isinstance(warnings, list):
        result["warnings"] = [*warnings, note]
    else:
        result["warnings"] = f"{warnings}; {note}" if warnings else note
    return result


async def tool_grep(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Search for a pattern using ripgrep or Python fallback; stops at MAX_SEARCH_RESULTS lines or SEARCH_BUDGET_S.

    Lines from credential files (``search_withheld``: dotenv files, key directories, the daemon's env file, the
    credential stores of ``$HOME`` and, searching ``$HOME``, its dot folders) are never returned (see _grep_line), and
    a sensitive ``path`` is refused here: the permission engine only sees ``path``, while an include glob or a
    repository's own ``.ignore`` (``!.env``) reopens hidden files to rg, and the fallback walks them anyway."""
    pattern = arguments["pattern"]
    path = _resolve_path(arguments.get("path", "."), cwd)
    include = arguments.get("include")
    exclude = arguments.get("exclude")
    if sensitive_path(str(path)):
        return {"error": f"Hardline deny (sensitive-path): {path}"}
    try:
        # --null: a NUL after each file name, so a credential file is told apart even with ':' in its path
        cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--null"]
        cmd += [f"--max-columns={_RG_MAX_COLUMNS}", "--max-columns-preview"]
        if include:
            for inc in include if isinstance(include, list) else [include]:
                cmd += ["-g", inc]
        # the last matching glob wins: the excludes come after include, so `include: "*"` cannot bring node_modules back
        for glob in _RG_EXCLUDES:
            cmd += ["-g", glob]
        if exclude:
            for exc in exclude if isinstance(exclude, list) else [exclude]:
                cmd += ["-g", f"!{exc}"]
        # -e and -- keep a pattern such as "--files" a literal search term, never an rg option
        cmd += ["-e", pattern, "--", str(path)]
        # rg anchors a -g glob that holds a slash at its working directory: run it where the search starts
        root = path if path.is_dir() else path.parent
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
            cwd=root if root.is_dir() else None,
        )
        assert proc.stdout is not None and proc.stderr is not None
        lines: list[str] = []
        withheld = 0
        cut = ""
        verdicts: dict[bytes, bool] = {}  # per file name rg printed: is it a sensitive path
        secret_file = search_withheld(str(path))
        deadline = time.monotonic() + SEARCH_BUDGET_S
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                cut = f"search stopped after {SEARCH_BUDGET_S:.0f} s with {len(lines)} matching lines so far: {_NARROW}"
                break
            if not raw:
                break
            line, secret = _grep_line(raw.rstrip(b"\n"), verdicts, secret_file)
            if secret:
                withheld += 1
                continue
            lines.append(line)
            if len(lines) > MAX_SEARCH_RESULTS:  # one past the cap proves the cut; stop rg instead of reading on
                lines = lines[:MAX_SEARCH_RESULTS]
                cut = f"truncated to the first {MAX_SEARCH_RESULTS} matching lines: {_NARROW}"
                break
        if cut:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()  # not wait(): it only returns once the pipes drain, and a full one never would
            result: dict[str, Any] = {"matches": "\n".join(lines).strip(), "warnings": cut}
        else:
            stderr = await proc.stderr.read()
            await proc.wait()
            matches = "\n".join(lines).strip()
            if proc.returncode == 2 and (matches or withheld):
                # rg exits 2 when any entry was unreadable (a root-owned dir) but still prints every other match
                note = stderr.decode("utf-8", errors="replace").strip().splitlines()[:3]
                result = {"matches": matches, "warnings": note}
            elif proc.returncode not in (0, 1):  # 1 = no matches
                raise RuntimeError(stderr.decode("utf-8", errors="replace"))
            else:
                result = {"matches": matches}
        if withheld:
            _note_withheld(
                result, f"{withheld} matching line(s) in credential files (.env, keys, home dot folders) not shown"
            )
        return result
    except (FileNotFoundError, RuntimeError) as rg_failure:
        # Python fallback (no rg, or a pattern rg refuses such as look-around): off the event loop, same tree rules as
        # glob, capped
        import re

        try:
            return await asyncio.to_thread(_grep_fallback, pattern, path, include, exclude)
        except re.error:
            if isinstance(rg_failure, RuntimeError):  # a pattern neither engine takes: rg's message names the fault
                raise rg_failure from None
            raise


#: Directories glob and the grep fallback never enter, even inside a git work tree where .gitignore may not list
#: them: dependencies, virtualenvs and caches are never source.
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        ".cache",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)
#: Outside a work tree nothing filters build output either (ripgrep-like defaults); inside one a tracked ``build/``
#: or ``target/`` is source, and .gitignore already drops an untracked one.
WALK_SKIP_DIRS = SKIP_DIRS | {"build", "target"}
#: Sub-paths never entered at any depth: other sessions' worktrees and background-job scratch directories.
SKIP_SUBPATHS = (".claude/worktrees", ".claude/jobs")
#: Most paths a glob returns, and most lines grep returns; past it the result says it was cut.
MAX_SEARCH_RESULTS = 1000
#: Wall-clock budget of one glob or grep; past it the partial result comes back with a note to narrow the search.
SEARCH_BUDGET_S = 10.0
_NARROW = "narrow the pattern or pass a smaller path (a project directory, not $HOME)"


def _skipped_subpath(rel: str) -> bool:
    return any(rel == s or rel.endswith("/" + s) or rel.startswith(s + "/") or f"/{s}/" in rel for s in SKIP_SUBPATHS)


def _git_files(path: Path) -> list[str] | None:
    """Files under ``path`` that git tracks or would track (``.gitignore`` honoured), relative to ``path``; None when
    ``path`` is not in a git work tree or git is missing. An index entry whose file is gone (deleted or moved without
    staging) is listed too: callers stat only the paths they keep, since one stat per listed file took minutes for a
    big checkout on WSL's /mnt/c."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(path), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=SEARCH_BUDGET_S,
            check=False,
            env=child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [f for f in out.stdout.decode("utf-8", errors="replace").split("\0") if f]


def _iter_tree(path: Path, deadline: float) -> Iterator[tuple[str | None, bool]]:
    """Yield ``(relative posix path, is_dir)`` under ``path``: from git when ``path`` is in a work tree (so
    ``.gitignore`` holds; that listing may name a file deleted from the work tree, see ``_git_files``), else from a
    walk pruning WALK_SKIP_DIRS and SKIP_SUBPATHS. Past ``deadline`` (``time.monotonic``) it yields ``(None, False)``
    once and stops, so the caller can say the result is partial."""
    tracked = _git_files(path)
    if tracked:  # empty: not a work tree, or a path git ignores as a whole (asked for on purpose): walk it
        files = [f for f in tracked if not SKIP_DIRS.intersection(f.split("/")[:-1]) and not _skipped_subpath(f)]
        dirs = sorted({"/".join(f.split("/")[:i]) for f in files for i in range(1, f.count("/") + 1)})
        for entries, is_dir in ((files, False), (dirs, True)):
            for rel in entries:
                if time.monotonic() > deadline:
                    yield None, False
                    return
                yield rel, is_dir
        return
    if shutil.which("rg"):
        yield from _rg_files(path, deadline)
        return
    for root, subdirs, names in os.walk(path):
        if time.monotonic() > deadline:
            yield None, False
            return
        rel = Path(root).relative_to(path).as_posix()
        prefix = "" if rel == "." else rel + "/"
        subdirs[:] = sorted(d for d in subdirs if d not in WALK_SKIP_DIRS and not _skipped_subpath(prefix + d))
        yield from ((prefix + d, True) for d in subdirs)
        yield from ((prefix + n, False) for n in sorted(names))


def _rg_files(path: Path, deadline: float) -> Iterator[tuple[str | None, bool]]:
    """``_iter_tree`` outside a work tree via ``rg --files``: parallel, honours every nested .gitignore, and lists all
    of a home directory in about two seconds where a Python walk needs minutes. Directories come from file paths."""
    import subprocess

    cmd = ["rg", "--files", "--hidden", "--no-require-git", "--no-messages"]
    for d in sorted(WALK_SKIP_DIRS):
        cmd += ["-g", f"!{d}/"]
    for sub in SKIP_SUBPATHS:
        cmd += ["-g", f"!**/{sub}/"]
    proc = subprocess.Popen(
        cmd, cwd=path, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_env()
    )
    assert proc.stdout is not None
    seen: set[str] = set()
    try:
        for raw in proc.stdout:
            if time.monotonic() > deadline:
                yield None, False
                return
            rel = raw.decode("utf-8", errors="replace").rstrip("\n").removeprefix("./")
            parts = rel.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in seen:
                    seen.add(d)
                    yield d, True
            yield rel, False
    finally:
        proc.kill()
        proc.wait()


def _tree(path: Path) -> tuple[list[str], list[str]]:
    """(files, directories) under ``path`` as relative posix paths (see ``_iter_tree``), within the search budget."""
    files: list[str] = []
    dirs: list[str] = []
    for rel, is_dir in _iter_tree(path, time.monotonic() + SEARCH_BUDGET_S):
        if rel is not None:
            (dirs if is_dir else files).append(rel)
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
    return items[:MAX_SEARCH_RESULTS], f"truncated to the first {MAX_SEARCH_RESULTS} results: {_NARROW}"


def _glob_sync(path: Path, pattern: str) -> dict[str, Any]:
    import fnmatch
    import re

    # The last pattern segment must match the basename: one compiled regex rejects nearly every path of a big tree
    # before the recursive matcher runs (a home directory has close to a million entries).
    last = [p for p in pattern.split("/") if p not in ("", ".")][-1:] or ["**"]
    basename_ok = (lambda _: True) if last[0] == "**" else re.compile(fnmatch.translate(last[0])).match
    found: list[str] = []
    timed_out = False
    for rel, _ in _iter_tree(path, time.monotonic() + SEARCH_BUDGET_S):
        if rel is None:
            timed_out = True
            break
        # the work tree decides on a hit the index still lists (see _git_files); a stat per listed path would not
        if basename_ok(rel.rpartition("/")[2]) and _glob_match(rel, pattern) and os.path.lexists(path / rel):
            found.append(rel)
            if len(found) > MAX_SEARCH_RESULTS:  # one past the cap proves the cut; no need to walk further
                break
    files, note = _capped(sorted(found))
    if timed_out:
        note = f"search stopped after {SEARCH_BUDGET_S:.0f} s with {len(files)} results so far: {_NARROW}"
    return {"files": files, **({"note": note} if note else {})}


#: The grep fallback treats a file with a NUL byte in its first _BINARY_SNIFF_BYTES as binary (as ripgrep does), reads
#: in pieces of at most _GREP_PIECE_BYTES and looks at the clock every _GREP_CLOCK_BYTES: read_text of a 300 MB binary
#: took 2.6 GB of memory and ran past the time budget.
_BINARY_SNIFF_BYTES = 8192
_GREP_PIECE_BYTES = 1 << 20
_GREP_CLOCK_BYTES = 64 * 1024


def _grep_file(fp: Path, rx: Any, deadline: float, *, named: bool = False) -> Iterator[str | None]:
    """``path:line:text`` for each line of ``fp`` that ``rx`` matches, streamed, cut to _RG_MAX_COLUMNS like rg's
    preview. A binary file found in a walk is skipped; one ``named`` on purpose gives a single ``path: binary file
    matches`` line, as rg says. Past ``deadline`` (``time.monotonic``) it yields None once and stops. A line longer than
    _GREP_PIECE_BYTES is searched piece by piece under its own line number, so a match across a piece edge, or an
    anchor at one, is approximate."""
    with fp.open("rb") as f:
        binary = b"\0" in f.read(_BINARY_SNIFF_BYTES)
        if binary and not named:
            return
        f.seek(0)
        number, line_start, unclocked = 0, True, 0
        for raw in iter(lambda: f.readline(_GREP_PIECE_BYTES), b""):
            unclocked += len(raw)
            if unclocked >= _GREP_CLOCK_BYTES:
                unclocked = 0
                if time.monotonic() > deadline:
                    yield None
                    return
            if line_start:  # a piece of a longer line keeps that line's number
                number += 1
            line_start = raw.endswith(b"\n")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not rx.search(line):
                continue
            if binary:
                yield f"{fp}: binary file matches"
                return
            if len(line) > _RG_MAX_COLUMNS:
                line = line[:_RG_MAX_COLUMNS] + " [... omitted end of long line]"
            yield f"{fp}:{number}:{line}"


def _search_glob_match(rel: str, pattern: str) -> bool:
    """rg ``-g`` semantics: a glob without a slash matches the file name at any depth, one with a slash is anchored at
    the searched directory (``tests/*.py`` is not ``pkg/tests/x.py``; ``**/tests/*.py`` is)."""
    import fnmatch

    if "/" not in pattern:
        return fnmatch.fnmatchcase(rel.rpartition("/")[2], pattern)
    return _match_parts(rel.split("/"), pattern.lstrip("/").split("/"))


def _grep_fallback(pattern: str, path: Path, include: Any, exclude: Any) -> dict[str, Any]:
    import re

    includes = include if isinstance(include, list) else [include] if include else []
    excludes = exclude if isinstance(exclude, list) else [exclude] if exclude else []
    rx = re.compile(pattern)
    deadline = time.monotonic() + SEARCH_BUDGET_S
    named = path.is_file()
    if named:
        base, candidates = path.parent, iter([(path.name, False)])
    else:
        base, candidates = path, _iter_tree(path, deadline)
    matches: list[str] = []
    skipped = 0
    timed_out = False
    secret_file = sensitive_path if named else search_withheld(str(path))  # a named file: tool_grep decided on it
    for rel, is_dir in candidates:
        if rel is None or time.monotonic() > deadline:
            timed_out = True
            break
        if is_dir:
            continue
        fp = base / rel
        if includes and not any(_search_glob_match(rel, p) for p in includes):
            continue
        if excludes and any(_search_glob_match(rel, p) for p in excludes):
            continue
        if secret_file(str(fp)):  # the tree walk lists hidden files too: dotenv files and key dirs stay out
            skipped += 1
            continue
        try:
            for hit in _grep_file(fp, rx, deadline, named=named):
                if hit is None:
                    timed_out = True
                    break
                matches.append(hit)
                if len(matches) > MAX_SEARCH_RESULTS:
                    break
        except OSError:  # unreadable, or an index entry deleted from the work tree (see _git_files)
            continue
        if timed_out or len(matches) > MAX_SEARCH_RESULTS:
            break
    shown, note = _capped(matches)
    if timed_out:
        note = f"search stopped after {SEARCH_BUDGET_S:.0f} s with {len(shown)} matching lines so far: {_NARROW}"
    result = {"matches": "\n".join(shown), **({"warnings": note} if note else {})}
    if skipped:
        _note_withheld(result, f"{skipped} credential file(s) (.env, keys, home dot folders) not searched")
    return result


async def tool_glob(arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """Find files matching a glob pattern (skipping .git, node_modules, .venv, dist and what .gitignore ignores;
    outside a git tree also caches and build output). Stops at MAX_SEARCH_RESULTS or SEARCH_BUDGET_S."""
    pattern = arguments["pattern"]
    path = _resolve_path(arguments.get("path", "."), cwd)
    return await asyncio.to_thread(_glob_sync, path, pattern)


TODO_STATUSES = ("pending", "in_progress", "completed")
_TODO_MARK = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
MAX_TODOS = 100


def parse_todos(raw: Any) -> tuple[list[dict[str, str]], str | None]:
    """The model's list as stored items ``{id, content, status}``, or an error. Items without an id get their
    1-based position; at most one item may be in progress."""
    if not isinstance(raw, list):
        return [], "todos must be a list of {content, status} items"
    if len(raw) > MAX_TODOS:
        return [], f"at most {MAX_TODOS} todos"
    todos: list[dict[str, str]] = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return [], f"todo {i} is not an object"
        content = " ".join(str(item.get("content") or "").split())
        status = str(item.get("status") or "")
        if not content:
            return [], f"todo {i} has no content"
        if status not in TODO_STATUSES:
            return [], f"todo {i} has status {status!r}; use one of {', '.join(TODO_STATUSES)}"
        todos.append({"id": str(item.get("id") or i), "content": content, "status": status})
    if sum(t["status"] == "in_progress" for t in todos) > 1:
        return [], "at most one todo may be in_progress"
    return todos, None


def render_todos(todos: list[dict[str, str]]) -> str:
    if not todos:
        return "Todo list cleared."
    done = sum(t["status"] == "completed" for t in todos)
    lines = [f"Todos ({done}/{len(todos)} done):"]
    lines += [f"{_TODO_MARK[t['status']]} {t['content']}" for t in todos]
    return "\n".join(lines)


class TodoStore:
    """The session's todo list. ``state`` is where it lives: the session's persisted meta in the gateway (key
    ``todos``), a plain dict for a loop of its own (REPL, headless)."""

    KEY = "todos"

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state if state is not None else {}

    @property
    def todos(self) -> list[dict[str, str]]:
        return list(self.state.get(self.KEY) or [])

    async def tool(self, arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
        """Replace the whole list (Claude Code's TodoWrite)."""
        todos, error = parse_todos(arguments.get("todos", arguments.get("items")))
        if error:
            return {"error": error}
        self.state[self.KEY] = todos
        return {"content": render_todos(todos), "todos": todos}


TODO_SPEC = ToolSpec(
    name="todo",
    description=(
        "Plan and track multi-step work. Send the whole list every time (it replaces the previous one): items "
        "{content, status: pending|in_progress|completed}. Keep exactly one item in_progress while working and mark "
        "items completed as soon as they are done.\n"
        f"Limits: at most {MAX_TODOS} items and one in_progress; the list lives with this session only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": list(TODO_STATUSES)},
                        "id": {"type": "string"},
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
    side_effect=False,
)


def register_todo(reg: ToolRegistry, state: dict[str, Any] | None = None) -> TodoStore:
    store = TodoStore(state)
    reg.register(TODO_SPEC, store.tool)
    return store


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
            "Limits: replaces the whole file atomically (an existing file keeps its mode); UTF-8 text only; an "
            "existing file must be read first and unchanged since.",
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
            "fallback); UTF-8 files only; line endings are kept; the file must be read first and unchanged since.",
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
    register_todo(reg)  # this loop's own list; the gateway re-registers it on the session's persisted state
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
