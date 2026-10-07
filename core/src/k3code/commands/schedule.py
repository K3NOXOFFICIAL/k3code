"""/schedule add "<cron | natural language>" <prompt> [--cwd DIR] [--model KEY] [--name NAME] | list | rm|pause|resume|run <id>."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k3code.automation.cronexpr import ScheduleError, parse_schedule
from k3code.automation.nlcron import nl_to_schedule
from k3code.commands import CommandDef
from k3code.commands._util import pop_option, reply, split_args

USAGE = (
    '/schedule add "<cron expr or text like \'every weekday at 9\'>" <prompt> [--cwd DIR] [--model KEY] [--name NAME]\n'
    "/schedule list | rm <id> | pause <id> | resume <id> | run <id>"
)


class ScheduleCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="schedule", help=USAGE, aliases=["cron"])

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        from k3code.automation.scheduler import format_jobs

        eng = await ctx.ensure_automation()
        args = split_args(arg)
        sub = args[0] if args else "list"
        if sub == "list":
            return reply(format_jobs(eng.db, eng.clock.now()))
        if sub in ("rm", "pause", "resume", "run") and len(args) == 2:
            fn = {"rm": eng.jobs.remove, "pause": eng.jobs.pause, "resume": eng.jobs.resume, "run": eng.jobs.run_now}[sub]
            ok = fn(args[1])
            done = {"rm": "Removed", "pause": "Paused", "resume": "Resumed", "run": "Running now:"}[sub]
            return reply(f"{done} {args[1]}." if ok else f"No such job: {args[1]}")
        if sub != "add":
            return reply(f"Usage: {USAGE}")
        args = args[1:]
        cwd = pop_option(args, "--cwd")
        model = pop_option(args, "--model") or ""
        name = pop_option(args, "--name") or ""
        if len(args) < 2:
            return reply(f"Usage: {USAGE}")
        when, prompt = args[0], " ".join(args[1:]).strip()
        live = ctx.sessions.get(session_id) if session_id else None
        workdir = str(Path(cwd).expanduser().resolve()) if cwd else (live.stored.cwd if live else str(Path.cwd()))
        try:
            sched = parse_schedule(when)
        except ScheduleError:
            try:
                sched = await nl_to_schedule(eng.runner.judge, when)
            except Exception as e:  # noqa: BLE001
                return reply(f"/schedule: {e}")
            answer = await ctx.clarify(
                f"“{when}” → `{sched.describe()}`. Create this job?", ["Yes", "No"], session_id
            )
            if str(answer.get("answer", "")).strip().lower() not in ("yes", "y"):
                return reply("Cancelled.")
        job = eng.jobs.add(prompt=prompt, schedule=sched, name=name, cwd=workdir, model=model)
        return reply(f"Scheduled {job['id']} “{job['name']}” ({sched.describe()}), runs as a background session in {workdir}.",
                     job=job["id"])
