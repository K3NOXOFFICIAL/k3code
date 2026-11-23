"""/jobs [list | status <job> | pause <job|all> | resume <job>] and /pause <job|all>: control the 24/7 k3jobs engine.

k3jobs lives next to the machine that runs the jobs; every call goes through its ``--json`` CLI contract
(``k3jobs <cmd> --json``, exit 0 ok / 3 unknown job / 4 probe failed / 5 refused), locally or over ssh
(``jobs.ssh_host``). Nothing here touches the KILL switch or systemd.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply, split_args

#: k3jobs job names (``new`` enforces the same grammar); fullmatch so a trailing newline is rejected too.
NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
MAX_SHOWN = 3000  # characters of output echoed back to the chat
LOG_TAIL_LINES = 8

USAGE = (
    "/jobs [list] | /jobs status <job> | /jobs pause <job|all> | /jobs resume <job>\n"
    "/pause <job|all>  (same as /jobs pause)"
)


@dataclass
class Call:
    code: int  # k3jobs exit code; 124 = timed out, 127 = binary missing, 255 = ssh itself failed
    doc: dict[str, Any] | None  # the single JSON document k3jobs printed, if it parsed
    stderr: str


def valid_name(name: str) -> bool:
    return NAME_RE.fullmatch(name) is not None


def _remote_program(path: str) -> str:
    """Quote ``path`` for the remote shell but keep a leading ``~/`` expandable (quoting it would stop expansion)."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def build_call(cfg: Any, args: list[str]) -> tuple[list[str], dict[str, str] | None]:
    """argv (+ env) for one k3jobs invocation; argv list only, never a local shell."""
    if not cfg.ssh_host:
        env = {**os.environ, "K3JOBS_ROOT": str(Path(cfg.root).expanduser())} if cfg.root else None
        return [str(Path(cfg.bin).expanduser()), *args, "--json"], env
    if cfg.ssh_host.startswith("-"):
        raise ValueError(f"jobs.ssh_host {cfg.ssh_host!r} looks like an ssh option")
    parts = [_remote_program(cfg.bin), *(shlex.quote(a) for a in args), "--json"]
    if cfg.root:
        parts = ["env", "K3JOBS_ROOT=" + _remote_program(cfg.root), *parts]
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", cfg.ssh_host, " ".join(parts)]
    return argv, None


async def run_k3jobs(cfg: Any, args: list[str]) -> tuple[int, str, str]:
    """Run one k3jobs call; tests replace this function, so no test spawns a process."""
    try:
        argv, env = build_call(cfg, args)
    except ValueError as e:
        return 126, "", str(e)  # bad jobs.* config: k3jobs never ran
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", f"not found: {argv[0]}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=cfg.timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):  # it may have exited while a child held the pipes open
            proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {cfg.timeout_s:g}s"
    code = proc.returncode if proc.returncode is not None else 1
    return code, out.decode(errors="replace"), err.decode(errors="replace")


async def call(cfg: Any, args: list[str]) -> Call:
    code, out, err = await run_k3jobs(cfg, args)
    doc: dict[str, Any] | None = None
    try:
        parsed = json.loads(out) if out.strip() else None
        doc = parsed if isinstance(parsed, dict) else None
    except ValueError:
        doc = None
    return Call(code, doc, err.strip())


def _detail(c: Call) -> str:
    msg = (c.doc or {}).get("message")
    if msg:
        return str(msg).strip()
    lines = c.stderr.splitlines()
    return lines[0].strip() if lines else ""


def explain(c: Call, cfg: Any, what: str) -> str:
    """One plain line for a failed call."""
    msg = _detail(c)
    if c.code == 5:
        return f"{what}: refused by k3jobs (KILL switch or policy); nothing changed. {msg}".strip()
    if c.code == 4:
        return f"{what}: model probe failed. {msg}".strip()
    if c.code == 126:
        return f"{what}: invalid jobs config: {msg}".strip()
    if c.code == 3:
        return f"{what}: unknown job. {msg}".strip()
    if c.code == 2:
        return f"{what}: k3jobs rejected the arguments. {msg}".strip()
    if c.code == 124:
        return f"{what}: {c.stderr or 'timed out'}"
    if c.code == 127:
        return f"{what}: k3jobs not found ({cfg.bin}); set jobs.bin or jobs.ssh_host."
    if c.code == 255 and cfg.ssh_host:
        return f"{what}: could not reach {cfg.ssh_host} over ssh. {msg}".strip()
    return f"{what}: k3jobs exited {c.code}. {msg}".strip()


NOT_ACKED = "k3jobs exited 0 but did not answer with an ok JSON document (is jobs.bin the k3jobs CLI?); state unknown."


def _acked(c: Call) -> bool:
    """A state-changing call counts as done only when k3jobs said so: exit 0 and a JSON document that is not ok:false."""
    return c.code == 0 and c.doc is not None and c.doc.get("ok") is not False


def _jobs_of(c: Call) -> list[dict[str, Any]]:
    jobs = (c.doc or {}).get("jobs")
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


BAD_LIST = "k3jobs answered, but the output is not JSON with a jobs list (is jobs.bin the k3jobs CLI?)."


def _bad_list(c: Call) -> bool:
    """A `list` that exited 0 without a usable job list must never read as 'no jobs running'."""
    return not isinstance((c.doc or {}).get("jobs"), list)


def _rate(v: Any) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) and not isinstance(v, bool) else "-"


def format_jobs(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "No jobs."
    rows = [("name", "state", "liveness", "ok 24h", "next run")]
    for j in jobs:
        state = "paused" if j.get("paused") else str(j.get("status") or "-")
        liveness, nxt = str(j.get("liveness") or "-"), str(j.get("next_run") or "-")
        rows.append((str(j.get("name", "?")), state, liveness, _rate(j.get("ok_rate_24h")), nxt))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip() for r in rows)


async def do_list(cfg: Any) -> str:
    c = await call(cfg, ["list"])
    if c.code != 0:
        return explain(c, cfg, "/jobs")
    if _bad_list(c):
        return f"/jobs: {BAD_LIST}"
    return format_jobs(_jobs_of(c))


async def do_status(cfg: Any, name: str) -> str:
    if not valid_name(name):
        return f"/jobs status: {name!r} is not a valid job name."
    c = await call(cfg, ["status", name])
    if c.code != 0 or c.doc is None:
        return explain(c, cfg, f"/jobs status {name}")
    job = c.doc.get("job") if isinstance(c.doc.get("job"), dict) else {}
    timer = c.doc.get("timer") if isinstance(c.doc.get("timer"), dict) else {}
    state = "paused" if job.get("paused") else str(job.get("status") or "-")
    lines = [
        f"{name}: {state}, liveness {job.get('liveness') or '-'}, model {job.get('model') or '-'}, "
        f"every {job.get('interval') or '-'}",
        f"ok 24h {_rate(job.get('ok_rate_24h'))} ({job.get('ok_24h', 0)}/{job.get('runs_24h', 0)}), "
        f"failures in a row {job.get('consecutive_failures', 0)}, open tasks {job.get('open_tasks', 0)}, "
        f"timer {'on' if timer.get('enabled') else 'off'}",
    ]
    runs = c.doc.get("runs")
    if isinstance(runs, list) and runs:
        lines.append("recent runs:")
        for r in [r for r in runs if isinstance(r, dict)][-5:]:
            line = f"  {r.get('ts', '?')}  {r.get('status', '?')}  {r.get('model') or '-'}  {r.get('error') or ''}"
            lines.append(line.rstrip())
    tail = str(c.doc.get("log_tail") or "").strip().splitlines()[-LOG_TAIL_LINES:]
    if tail:
        lines += ["log tail:", *(f"  {ln[:200]}" for ln in tail)]
    return "\n".join(lines)[:MAX_SHOWN]


async def do_pause(cfg: Any, name: str) -> str:
    if not valid_name(name):
        return f"/pause: {name!r} is not a valid job name."
    c = await call(cfg, ["pause", name])
    if c.code != 0:
        return explain(c, cfg, f"/pause {name}")
    if not _acked(c):
        return f"/pause {name}: {NOT_ACKED}"
    return f"Paused {name}. /jobs resume {name} to start it again."


async def do_pause_all(cfg: Any) -> str:
    c = await call(cfg, ["list"])
    if c.code != 0:
        return explain(c, cfg, "/pause all")
    if _bad_list(c):
        return f"/pause all: {BAD_LIST} Nothing was paused."
    todo = [str(j["name"]) for j in _jobs_of(c) if isinstance(j.get("name"), str) and not j.get("paused")]
    if not todo:
        return "Nothing to pause: every job is already paused."
    lines: list[str] = []
    paused = 0
    for name in todo:
        if not valid_name(name):
            lines.append(f"  {name}: skipped (unexpected job name)")
            continue
        r = await call(cfg, ["pause", name])
        if _acked(r):
            paused += 1
            lines.append(f"  {name}: paused")
        elif r.code == 0:
            lines.append(f"  {name}: {NOT_ACKED}")
        else:
            lines.append("  " + explain(r, cfg, name))
    return "\n".join([f"Paused {paused} of {len(todo)} running jobs.", *lines])[:MAX_SHOWN]


async def do_resume(cfg: Any, name: str) -> str:
    if not valid_name(name):
        return f"/jobs resume: {name!r} is not a valid job name."
    c = await call(cfg, ["resume", name])
    if c.code != 0:
        return explain(c, cfg, f"/jobs resume {name}")
    if not _acked(c):
        return f"/jobs resume {name}: {NOT_ACKED}"
    extra = str((c.doc or {}).get("message") or "").strip()
    return f"Resumed {name}.{(' ' + extra) if extra else ''}"


async def running_summary(cfg: Any) -> str:
    c = await call(cfg, ["list"])
    if c.code != 0:
        return explain(c, cfg, "/pause")
    if _bad_list(c):
        return f"/pause: {BAD_LIST}"
    names = [str(j.get("name")) for j in _jobs_of(c) if not j.get("paused")]
    return "Running jobs: " + (", ".join(names) if names else "none")


class JobsCommand(CommandDef):
    headless = True  # reads only ctx.config, so `k3code -p "/jobs"` works from a script or cron without a server
    needs_server = False

    def __init__(self) -> None:
        super().__init__(name="jobs", help=USAGE)

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        cfg = ctx.config.jobs
        args = split_args(arg)
        sub = args[0] if args else "list"
        rest = args[1:]
        if sub == "list" and not rest:
            return reply(await do_list(cfg))
        if sub == "status" and len(rest) == 1:
            return reply(await do_status(cfg, rest[0]))
        if sub == "pause" and len(rest) == 1:
            return reply(await (do_pause_all(cfg) if rest[0] == "all" else do_pause(cfg, rest[0])))
        if sub == "resume" and len(rest) == 1:
            return reply(await do_resume(cfg, rest[0]))
        return reply(f"Usage: {USAGE}")


class PauseCommand(CommandDef):
    """``/pause`` is its own command, not an alias: dispatch does not tell a handler which name was typed."""

    headless = True
    needs_server = False

    def __init__(self) -> None:
        super().__init__(name="pause", help="/pause <job|all>: pause a 24/7 job (resume with /jobs resume <job>)")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        cfg = ctx.config.jobs
        args = split_args(arg)
        if not args:
            return reply(f"Usage: /pause <job|all>\n{await running_summary(cfg)}")
        if len(args) != 1:
            return reply("Usage: /pause <job|all>")
        return reply(await (do_pause_all(cfg) if args[0] == "all" else do_pause(cfg, args[0])))
