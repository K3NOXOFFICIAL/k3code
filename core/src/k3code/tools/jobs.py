"""Background shell jobs: ``bash`` with ``background: true`` starts one, ``bash_output`` reads what it printed since
the last read, ``bash_kill`` stops it. A job belongs to the session that started it: another session can neither read
nor kill it. Every job of a session is killed when the session closes (``reap``).

``list_jobs(session_id)`` is the read-only view for callers outside the agent loop. ``processes(session_id)`` is the
gateway's ``process.list``: the session's background jobs plus the foreground ``bash`` commands running right now
(``track``/``untrack`` around each one).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from dataclasses import dataclass, field
from typing import Any

#: Unread output kept per job; past it the oldest unread bytes are dropped (and the next read says how many).
MAX_UNREAD_BYTES = 1024 * 1024
#: Last output bytes kept per job for the process list's preview (independent of what bash_output has read).
TAIL_BYTES = 2048


@dataclass
class Job:
    id: str
    session_id: str
    command: str
    proc: asyncio.subprocess.Process
    started: float = field(default_factory=time.time)
    unread: bytearray = field(default_factory=bytearray)
    dropped: int = 0  # unread bytes dropped since the last read
    exit_code: int | None = None
    reader: asyncio.Task[None] | None = None
    cwd: str = ""
    exited_at: float | None = None
    tail: bytearray = field(default_factory=bytearray)

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def feed(self, chunk: bytes) -> None:
        self.unread += chunk
        over = len(self.unread) - MAX_UNREAD_BYTES
        if over > 0:
            del self.unread[:over]
            self.dropped += over
        self.tail += chunk
        del self.tail[:-TAIL_BYTES]

    def info(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "session_id": self.session_id,
            "command": self.command,
            "pid": self.proc.pid,
            "running": self.running,
            "exit_code": self.exit_code,
            "started": self.started,
        }

    def process_entry(self, now: float) -> dict[str, Any]:
        """The TUI's ``ProcessEntry`` shape (tui/src/app/processRoster.ts); ``session_id`` is the row id there."""
        return {
            "session_id": self.id,
            "job_id": self.id,
            "kind": "background",
            "pid": self.proc.pid,
            "command": self.command,
            "cwd": self.cwd,
            "started": self.started,
            "status": "running" if self.running else "exited",
            "uptime_seconds": max(0.0, now - self.started),
            "exit_code": self.exit_code,
            "exited_at": self.exited_at,
            "completion_reason": None,
            "output_preview": bytes(self.tail).decode("utf-8", errors="replace"),
        }


@dataclass
class Foreground:
    """A foreground ``bash`` command while it runs (it leaves the list when the call returns)."""

    session_id: str
    command: str
    pid: int
    cwd: str
    started: float = field(default_factory=time.time)

    def process_entry(self, now: float) -> dict[str, Any]:
        return {
            "session_id": f"fg{self.pid}",
            "job_id": "",
            "kind": "foreground",
            "pid": self.pid,
            "command": self.command,
            "cwd": self.cwd,
            "started": self.started,
            "status": "running",
            "uptime_seconds": max(0.0, now - self.started),
            "exit_code": None,
            "exited_at": None,
            "completion_reason": None,
            "output_preview": "",
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._foreground: dict[int, Foreground] = {}

    def add(self, session_id: str, command: str, proc: asyncio.subprocess.Process, *, cwd: str = "") -> Job:
        job = Job(id=f"job{next(self._ids)}", session_id=session_id, command=command, proc=proc, cwd=cwd)
        job.reader = asyncio.ensure_future(self._pump(job))
        self._jobs[job.id] = job
        return job

    async def _pump(self, job: Job) -> None:
        stream = job.proc.stdout
        if stream is not None:
            while chunk := await stream.read(64 * 1024):
                job.feed(chunk)
        job.exit_code = await job.proc.wait()
        job.exited_at = time.time()

    def get(self, session_id: str, job_id: str) -> Job | None:
        """The job, only for the session that started it."""
        job = self._jobs.get(job_id)
        return job if job is not None and job.session_id == session_id else None

    async def read(self, session_id: str, job_id: str) -> dict[str, Any]:
        job = self.get(session_id, job_id)
        if job is None:
            return {"error": f"no job {job_id!r} in this session (jobs: {self._ids_of(session_id) or 'none'})"}
        await asyncio.sleep(0)  # let the reader take what the pipe already holds
        text = bytes(job.unread).decode("utf-8", errors="replace")
        job.unread.clear()
        dropped, job.dropped = job.dropped, 0
        state = f"running (pid {job.proc.pid})" if job.running else f"exited with code {job.exit_code}"
        parts = [f"[{job.id} {state}]"]
        if dropped:
            parts.append(f"[{dropped} bytes of output were dropped: read sooner to keep them]")
        parts.append(text.rstrip("\n") if text else "(no new output)")
        return {"content": "\n".join(parts)}

    async def kill(self, session_id: str, job_id: str) -> dict[str, Any]:
        job = self.get(session_id, job_id)
        if job is None:
            return {"error": f"no job {job_id!r} in this session (jobs: {self._ids_of(session_id) or 'none'})"}
        if not job.running:
            return {"content": f"{job.id} had already exited with code {job.exit_code}"}
        await _stop(job)
        return {"content": f"killed {job.id} (exit code {job.exit_code})"}

    async def reap(self, session_id: str) -> int:
        """Kill every job of ``session_id`` and forget them all; returns how many were still running."""
        mine = [j for j in self._jobs.values() if j.session_id == session_id]
        running = [j for j in mine if j.running]
        await asyncio.gather(*(_stop(j) for j in running), return_exceptions=True)
        for j in mine:
            self._jobs.pop(j.id, None)
        return len(running)

    async def reap_all(self) -> int:
        n = 0
        for sid in {j.session_id for j in self._jobs.values()}:
            n += await self.reap(sid)
        return n

    def list_jobs(self, session_id: str) -> list[dict[str, Any]]:
        return [j.info() for j in self._jobs.values() if j.session_id == session_id]

    def track(self, session_id: str, command: str, pid: int, cwd: str = "") -> None:
        """A foreground command of ``session_id`` started (shown by :meth:`processes` until :meth:`untrack`)."""
        self._foreground[pid] = Foreground(session_id=session_id, command=command, pid=pid, cwd=cwd)

    def untrack(self, pid: int) -> None:
        self._foreground.pop(pid, None)

    def processes(self, session_id: str) -> list[dict[str, Any]]:
        """Foreground commands running now, then the session's background jobs (running and exited)."""
        now = time.time()
        fg = [f.process_entry(now) for f in self._foreground.values() if f.session_id == session_id]
        return fg + [j.process_entry(now) for j in self._jobs.values() if j.session_id == session_id]

    def _ids_of(self, session_id: str) -> str:
        return ", ".join(j.id for j in self._jobs.values() if j.session_id == session_id)


async def _stop(job: Job) -> None:
    from k3code.tools import _kill_group

    await _kill_group(job.proc)
    if job.reader is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(job.reader, timeout=5.0)
    if job.exit_code is None:
        job.exit_code = job.proc.returncode
    if job.exited_at is None:
        job.exited_at = time.time()


#: The process-wide registry (the gateway serves every session from one event loop).
REGISTRY = JobRegistry()


def list_jobs(session_id: str) -> list[dict[str, Any]]:
    """Background jobs of one session: job_id, command, pid, running, exit_code, started (epoch seconds)."""
    return REGISTRY.list_jobs(session_id)


async def reap(session_id: str) -> int:
    return await REGISTRY.reap(session_id)
