"""/automations list | add [yaml] | rm|pause|resume|test <id> | suggest [accept|dismiss <n|id>]."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from k3code.automation.automations import ACTION_TYPES, TRIGGER_TYPES, AutomationError
from k3code.commands import CommandDef
from k3code.commands._util import reply, split_args

USAGE = (
    "/automations list | add [<yaml>] | rm|pause|resume|test <id> | suggest [accept|dismiss <n|id>]\n"
    "yaml: {name: tests, trigger: {type: file_change, glob: 'src/**/*.py', debounce: 3}, "
    "action: {type: prompt, prompt: 'run the tests'}, policy: {cooldown_s: 60}}\n"
    f"triggers: {', '.join(TRIGGER_TYPES)}; actions: {', '.join(ACTION_TYPES)}"
)

# Wizard choices (the client may also accept free text for any question).
_SCHEDULES = ["0 2 * * *", "30 8 * * 1-5", "0 * * * *", "*/30 * * * *"]
_GLOBS = ["**/*.py", "src/**", "**/*"]
_PROMPTS = ["Run the tests and report failures", "Summarise what changed and flag risks"]


class AutomationsCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="automations", help=USAGE, aliases=["automation", "auto"])

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        eng = await ctx.ensure_automation()
        mgr = eng.automations
        args = split_args(arg)
        sub = args[0] if args else "list"
        live = ctx.sessions.get(session_id) if session_id else None
        cwd = (live.stored.cwd if live else "") or str(Path.cwd())
        if sub == "list":
            rows = mgr.db.rows("automations")
            if not rows:
                return reply("No automations. Try /automations suggest, or /automations add.")
            return reply("\n".join(mgr.describe(r) for r in rows))
        if sub in ("rm", "pause", "resume") and len(args) == 2:
            fn = {"rm": mgr.remove, "pause": mgr.pause, "resume": mgr.resume}[sub]
            ok = await fn(args[1])
            return reply(
                f"{ {'rm': 'Removed', 'pause': 'Paused', 'resume': 'Resumed'}[sub] } {args[1]}."
                if ok
                else f"No such automation: {args[1]}"
            )
        if sub == "test" and len(args) == 2:
            return reply(await mgr.test(args[1]))
        if sub == "suggest":
            return await self._suggest(eng, args[1:], cwd)
        if sub == "add":
            body = arg.split(None, 1)[1] if len(arg.split(None, 1)) > 1 else ""
            try:
                spec = yaml.safe_load(body) if body.strip() else await self._wizard(ctx, session_id)
            except yaml.YAMLError as e:
                return reply(f"Invalid YAML: {e}")
            if not isinstance(spec, dict):
                return reply("Cancelled." if spec is None else f"Expected a YAML mapping.\n{USAGE}")
            try:
                row = mgr.add(
                    name=str(spec.get("name") or ""),
                    trigger=dict(spec.get("trigger") or {}),
                    action=dict(spec.get("action") or {}),
                    policy=dict(spec.get("policy") or {}),
                    cwd=str(spec.get("cwd") or cwd),
                )
            except AutomationError as e:
                return reply(f"/automations add: {e}")
            extra = ""
            if row["trigger"]["type"] == "webhook" and eng.automations.webhook:
                extra = (
                    f"\nPOST http://127.0.0.1:{eng.automations.webhook.port}/hook/{row['id']} with header "
                    f"`X-K3-Token: {row['trigger']['token']}`"
                )
            return reply(f"Added automation {row['id']} “{row['name']}”.{extra}", automation=row["id"])
        return reply(f"Usage: {USAGE}")

    async def _suggest(self, eng: Any, args: list[str], cwd: str) -> dict[str, Any]:
        sug = eng.suggestions
        if len(args) == 2 and args[0] in ("accept", "dismiss"):
            if args[0] == "dismiss":
                return reply(
                    "Dismissed (won't be offered again)." if sug.dismiss(args[1]) else "No such pending suggestion."
                )
            s = sug.mark_accepted(args[1])
            if s is None:
                return reply("No such pending suggestion.")
            spec = s["spec"]
            row = eng.automations.add(
                name=spec["name"], trigger=spec["trigger"], action=spec["action"], policy=spec.get("policy"), cwd=cwd
            )
            return reply(f"Created automation {row['id']} “{row['name']}” from suggestion “{s['title']}”.")
        pending = sug.suggest()
        if not pending:
            return reply("No suggestions right now.")
        lines = [f"{i}. {s['title']} — {s['description']}" for i, s in enumerate(pending, 1)]
        return reply("\n".join(lines) + "\n/automations suggest accept <n>  |  dismiss <n>")

    async def _wizard(self, ctx: Any, session_id: str | None) -> dict[str, Any] | None:
        async def ask(q: str, choices: list[str]) -> str:
            return str((await ctx.clarify(q, choices, session_id)).get("answer", "")).strip()

        ttype = await ask("When should it run? (trigger)", list(TRIGGER_TYPES))
        if ttype not in TRIGGER_TYPES:
            return None
        trigger: dict[str, Any] = {"type": ttype}
        if ttype == "cron":
            trigger["schedule"] = await ask("Cron expression?", _SCHEDULES)
        elif ttype == "file_change":
            trigger["glob"] = await ask("Which files? (glob)", _GLOBS)
        elif ttype == "session_event":
            trigger["event"] = await ask("On which session event?", ["completed", "failed", "needs_input"])
        elif ttype == "idle":
            trigger["minutes"] = float(await ask("Idle for how many minutes?", ["10", "30", "60"]) or 30)
        elif ttype == "git":
            trigger["event"] = await ask("Git event?", ["commit", "checkout"])
        atype = await ask("What should it do? (action)", list(ACTION_TYPES))
        if atype not in ACTION_TYPES:
            return None
        key = {"prompt": "prompt", "shell": "command", "notify": "text", "goal": "objective"}[atype]
        choices = (
            _PROMPTS if atype in ("prompt", "goal") else (["git status"] if atype == "shell" else ["Automation fired"])
        )
        action = {"type": atype, key: await ask(f"{key}?", choices)}
        name = await ask("Name?", [f"{ttype} → {atype}"])
        return {"name": name, "trigger": trigger, "action": action}
