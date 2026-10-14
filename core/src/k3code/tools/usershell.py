"""The user's own shell commands (``!cmd`` in the TUI): one runner with cancellation, a timeout, streaming and a
process-group kill that keeps the partial output.

Not sandboxed and not approved (the user typed it, as in a terminal); the caller applies the hardline rules.
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from k3code.tools import MAX_OUTPUT_BYTES, PIPE_GRACE_S, _Capture, _drain, _kill_group, _spawn_shell, jobs

#: A user's command gets a terminal's timeout, not an agent tool's.
USER_SHELL_TIMEOUT_S = 300.0
#: Per stream: bytes after which the command is killed (read at call time, so a test can lower it).
USER_SHELL_MAX_BYTES = MAX_OUTPUT_BYTES


@dataclass
class ShellRun:
    """What a command left: its output (the first 40 KB and the last 10 KB of each stream), its exit code (None when
    it never started; negative for a signal), how it ended, and a user-facing ``error`` line."""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool = False
    cancelled: bool = False
    overflowed: bool = False
    error: str = ""


def _size(n: int) -> str:
    return f"{n >> 20} MB" if n >= 1 << 20 else f"{n} bytes"


async def run_user_command(
    command: str,
    *,
    cwd: Path,
    session_id: str = "",
    timeout: float = USER_SHELL_TIMEOUT_S,
    cancel: asyncio.Event | None = None,
    on_output: Callable[[str, bytes], None] | None = None,
    extra_env: Mapping[str, str] | None = None,
    stdin: int | bytes = asyncio.subprocess.DEVNULL,
    pass_fds: Sequence[int] = (),
) -> ShellRun:
    """Run ``command`` under :func:`~k3code.reliability.sandbox.shell_path` in ``cwd``, in its own process group.

    Never raises for the command's sake: a timeout or ``cancel`` being set kills the whole group and returns the
    partial output with ``timed_out`` / ``cancelled``. Cancelling the awaiting task (a client that disconnects, a
    daemon shutdown) still kills the group: the kill is shielded. ``on_output(stream, chunk)`` sees every chunk as it
    is read (``stream`` is ``"stdout"`` or ``"stderr"``). The command shows in the process dock
    (``jobs.REGISTRY``) under ``session_id`` while it runs.

    ``stdin`` is closed (DEVNULL) by default; bytes are fed and then closed; an int is a file descriptor. ``extra_env``
    goes through ``child_env``, which removes the gateway's socket variables *after* applying it: a per-call channel
    (an askpass helper's) cannot be the daemon socket handed over in the environment. Pass it as one of ``pass_fds``,
    or as a one-shot path in an ``extra_env`` variable whose name ``child_env`` keeps.
    """
    feed = stdin if isinstance(stdin, bytes) else None
    try:
        proc = await _spawn_shell(
            command,
            cwd,
            None,
            env_extra=extra_env,
            stdin=asyncio.subprocess.PIPE if feed is not None else stdin,
            pass_fds=pass_fds,
        )
    except OSError as e:
        return ShellRun("", "", 127, error=f"could not start the command: {e}")
    jobs.REGISTRY.track(session_id, command, proc.pid, str(cwd))
    limit = USER_SHELL_MAX_BYTES
    out, err = _Capture(), _Capture()
    pending: set[asyncio.Future[Any]] = set()
    readers = [
        asyncio.ensure_future(
            _drain(proc.stdout, out, proc, on_chunk=on_output and partial(on_output, "stdout"), limit=limit)
        ),
        asyncio.ensure_future(
            _drain(proc.stderr, err, proc, on_chunk=on_output and partial(on_output, "stderr"), limit=limit)
        ),
    ]
    run = ShellRun("", "", None)
    try:
        if feed is not None and proc.stdin is not None:
            try:
                proc.stdin.write(feed)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the command did not read it all
            proc.stdin.close()
        waited = asyncio.ensure_future(proc.wait())
        pending.add(waited)
        if cancel is not None:
            pending.add(asyncio.ensure_future(cancel.wait()))
        done, _ = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if waited not in done:
            run.cancelled = bool(done)  # the cancel event, else the timeout
            run.timed_out = not done
            await _kill_group(proc)
        _, held = await asyncio.wait(readers, timeout=PIPE_GRACE_S)
        if held:  # the command ended but something it started still has its output: stop that too
            await _kill_group(proc)
            if not (run.cancelled or run.timed_out):
                run.error = "a background process it started was stopped when it ended"
        run.overflowed = out.overflowed or err.overflowed
        if run.timed_out:
            run.error = f"timed out after {timeout:g} s (stdin is closed: interactive programs are not supported)"
        elif run.overflowed:
            run.error = f"output over {_size(limit)}: stopped"
        return run
    finally:
        for task in (*pending, *readers):
            task.cancel()
        jobs.REGISTRY.untrack(proc.pid)
        if proc.returncode is None:  # an outer cancellation: the tree must not outlive it, even if cancelled again
            await asyncio.shield(_kill_group(proc))
        run.stdout, run.stderr, run.exit_code = out.text(), err.text(), proc.returncode


class ShellStreamer:
    """Live output of one command as events: coalesced to at most one every ``interval`` seconds while it runs, at
    most ``event_bytes`` of UTF-8 text per event, at most ``total_bytes`` in all; past that one ``{notice}`` event
    and silence (the reply carries the output). A flood can therefore never fill a client's backlog.

    ``feed`` is the runner's ``on_output``; ``run`` paces the events while the command runs; ``finish`` sends what is
    left at once (call it after the command ended, before the reply)."""

    NOTICE = "output continues; the rest arrives when the command ends"

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        interval: float = 0.1,
        event_bytes: int = 16 * 1024,
        total_bytes: int = 256 * 1024,
    ) -> None:
        self.emit, self.interval, self.event_bytes = emit, interval, event_bytes
        self.budget = total_bytes  # bytes still accepted
        self.capped = False
        self.noticed = False
        self.queue: list[tuple[str, bytearray]] = []  # (stream, bytes) in arrival order, same streams merged
        self.decoders = {name: codecs.getincrementaldecoder("utf-8")(errors="replace") for name in ("stdout", "stderr")}
        self.carry = {"stdout": "", "stderr": ""}

    def feed(self, stream: str, chunk: bytes) -> None:
        if self.capped:
            return
        if len(chunk) > self.budget:
            chunk, self.capped = chunk[: self.budget], True
        self.budget -= len(chunk)
        if not chunk:
            return
        if self.queue and self.queue[-1][0] == stream:
            self.queue[-1][1].extend(chunk)
        else:
            self.queue.append((stream, bytearray(chunk)))

    def _one(self) -> bool:
        """Send one event (text or the notice); False when there is nothing to send."""
        if self.noticed:
            return False
        while self.queue:
            stream, buf = self.queue[0]
            text = self.carry[stream] + self.decoders[stream].decode(bytes(buf[: self.event_bytes]))
            del buf[: self.event_bytes]
            if not buf:
                self.queue.pop(0)
            data = text.encode("utf-8")
            cut = min(len(data), self.event_bytes)
            while cut < len(data) and (data[cut] & 0xC0) == 0x80:  # back off to a character boundary
                cut -= 1
            self.carry[stream] = data[cut:].decode("utf-8")
            if data[:cut]:
                self.emit({"stream": stream, "text": data[:cut].decode("utf-8")})
                return True
        if self.capped and not self.noticed:
            self.noticed = True
            self.emit({"notice": self.NOTICE})
            return True
        return False

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self._one()

    def finish(self) -> None:
        while self._one():
            pass
        for name in ("stdout", "stderr"):  # a character cut at the very end, or text the last event had no room for
            rest = self.carry[name] + self.decoders[name].decode(b"", final=True)
            self.carry[name] = rest
            if rest and not self.noticed:
                self.queue.append((name, bytearray()))
                while self._one():
                    pass
