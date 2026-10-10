"""/ultraplan, /ultracode (and /go for an ultraplan'd plan).

Each command is split in two: ``prepare`` checks the input and returns either an immediate reply (usage, unavailable:
costs nothing) or a :class:`JobSpec`, and ``handle`` = ``prepare`` + ``ctx.start_job``. Wake words and the ultracode
mode (see ``GatewayServer._run_turn_locked``) call ``prepare`` too and run the spec inline in the running turn; they
pass ``context``, what the user's UserPromptSubmit hooks added, which a normal turn appends to the prompt as well.
The pipeline gets it apart from the task: it goes into the agents' prompts, never into a title or heading.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply


def _live(ctx: Any, session_id: str | None) -> Any:
    return ctx.sessions.get(session_id) if session_id else None


@dataclass
class JobSpec:
    """A command that runs as the session's turn: what to show, what to run, what the slash command answers."""

    label: str  #: the status line while it runs (and the user message of a slash command)
    factory: Callable[[], Awaitable[str]]  #: the work; its text becomes the assistant message
    ack: str  #: the slash command's immediate reply


def start_spec(ctx: Any, live: Any, spec: JobSpec | dict[str, Any]) -> dict[str, Any]:
    """The slash command's end of ``prepare``: start the job, answer with its ack (or the immediate reply)."""
    if isinstance(spec, dict):
        return spec
    try:
        ctx.start_job(live, spec.label, spec.factory)
    except Exception as e:  # noqa: BLE001
        return reply(str(e))
    return reply(spec.ack)


class UltraPlanCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="ultraplan", help="Deep plan: 3 independent planners + a judge: /ultraplan <task>")

    async def prepare(self, ctx: Any, live: Any, arg: str, *, context: str = "") -> JobSpec | dict[str, Any]:
        if not arg:
            return reply("Usage: /ultraplan <task>")

        async def job() -> str:
            up = await ctx.ultra.ultraplan(live, arg, context=context)
            live.stored.meta["ultra_plan"] = {"task": arg, "plan": up.plan, "path": str(up.path or "")}
            ctx.store.save(live.stored)
            ctx.ultra.show_plan(live, up)
            scores = ", ".join(f"{k} {v:g}" for k, v in up.scores.items())
            head = f"Plan for: {arg}\n(planners: {', '.join(up.angles)}{'; judge scores: ' + scores if scores else ''})"
            tail = (
                f"\n\nSaved to {up.path}. Run /go to execute it (independent steps fan out to parallel "
                "sub-agents in worktrees)."
            )
            return f"{head}\n\n{up.plan}{tail}" + (f"\n\nNote: {up.judge_note}" if up.judge_note else "")

        return JobSpec(
            f"/ultraplan {arg}",
            job,
            "Planning from three angles (MVP-first, risk-first, architecture-first), then judging…",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = _live(ctx, session_id)
        if live is None:
            return reply("No active session.")
        return start_spec(ctx, live, await self.prepare(ctx, live, arg))


#: ``/ultracode <word>``: when the whole argument is one of these words it sets or shows the mode; anything else is
#: the task (a one-shot run that leaves the mode alone).
_MODE_WORDS = {"on": "ultracode", "off": "off"}
#: words that may stand around a mode word in a typed request: "turn off ultracode", "ultracode mode off"
_MODE_FILLER = frozenset({"turn", "switch", "set", "show", "mode", "please", "to"})


def typed_mode_word(task: str) -> str | None:
    """``on``, ``off`` or ``status`` when what a wake word leaves of a prompt only asks for that, else None.

    "ultracode off" and "turn ultracode off" are mode control, the words ``/ultracode`` takes as such, not a task
    for the pipeline (they leave "off" and "turn off"). Only filler ("show ultracode mode") and a question ("is
    ultracode on?") ask for the status. A task that merely starts with one ("ultracode on the auth module") is a
    task."""
    typed = [w for w in (t.strip(".,;:!?") for t in task.lower().split()) if w]
    if not typed:
        return None  # the word alone: the command's usage line
    words = [w for w in typed if w not in _MODE_FILLER]
    if not words:
        return "status"
    if words[0] == "is" and len(words) == 2 and words[1] in _MODE_WORDS:
        return "status"  # asks whether it is on, does not switch it
    return words[0] if len(words) == 1 and (words[0] in _MODE_WORDS or words[0] == "status") else None


def _mode_text(ctx: Any, mode: str) -> str:
    from k3code.autonomy.ultra import ultra_cfg

    if mode == "ultracode":
        return (
            f"Ultracode mode is on: every prompt of at least '{ultra_cfg(ctx.config)['min_scope']}' scope runs the "
            "multi-agent pipeline (plan, fan-out, review panel, fixes, tests). Answers and one-line edits stay "
            "normal turns. /ultracode off turns it off."
        )
    return "Ultracode mode is off. /ultracode on turns it on; say ultracode in a prompt to run it once."


class UltraCodeCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="ultracode",
            help="Plan, fan out, adversarial review, fix, test: /ultracode <task> "
            "(budget: ultracode.max_tokens / max_agents). Bare /ultracode, on, off, status: the always-on mode",
        )

    async def prepare(self, ctx: Any, live: Any, arg: str, *, context: str = "") -> JobSpec | dict[str, Any]:
        if not arg:
            return reply("Usage: /ultracode <task>")
        return JobSpec(
            f"/ultracode {arg}",
            lambda: ctx.ultra.ultracode(live, arg, context=context),
            "ultracode started: plan → fan-out → review panel → fixes → tests. See the agent strip.",
        )

    @staticmethod
    def _set_mode(ctx: Any, live: Any, mode: str) -> dict[str, Any]:
        """The always-on mode (``ultra_mode``): kept in the session's meta, shown in ``session.info``."""
        live.ultra_mode = mode
        if mode == "off":
            live.stored.meta.pop("ultra_mode", None)
        else:
            live.stored.meta["ultra_mode"] = mode
        ctx.store.save(live.stored)
        live.emit("session.info", live.live_info())
        return reply(_mode_text(ctx, mode))

    @classmethod
    def apply_mode_word(cls, ctx: Any, live: Any, word: str) -> dict[str, Any]:
        """``on`` / ``off`` set the mode, ``status`` shows it (``/ultracode <word>``, or the word typed after the
        wake word)."""
        if word == "status":
            return reply(_mode_text(ctx, getattr(live, "ultra_mode", "off")))
        return cls._set_mode(ctx, live, _MODE_WORDS[word])

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = _live(ctx, session_id)
        if live is None:
            return reply("No active session.")
        word = arg.strip().lower()
        if not word:  # bare: flip the mode
            current = getattr(live, "ultra_mode", "off")
            return self._set_mode(ctx, live, "off" if current == "ultracode" else "ultracode")
        if word in _MODE_WORDS or word == "status":
            return self.apply_mode_word(ctx, live, word)
        return start_spec(ctx, live, await self.prepare(ctx, live, arg))


def go_for_ultraplan(live: Any) -> dict[str, Any] | None:
    """``/go`` after ``/ultraplan``: approve the plan for the next turn and send the task."""
    pending = live.stored.meta.get("ultra_plan") if live else None
    if not pending:
        return None
    live.preapproved_plan = dict(pending)
    live.stored.meta.pop("ultra_plan", None)
    task = str(pending["task"])
    where = f" ({Path(pending['path']).name})" if pending.get("path") else ""
    return {"type": "send", "message": task, "text": task, "notice": f"Executing the ultraplan{where}: {task}"}
