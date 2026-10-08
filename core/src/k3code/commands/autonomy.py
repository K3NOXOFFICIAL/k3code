"""M4a commands: /scope /proposals /preview /go /advisor."""

from __future__ import annotations

from typing import Any

from k3code.artifacts import write_artifact_file
from k3code.autonomy import advisor, autonomy_cfg
from k3code.autonomy import preview as preview_mod
from k3code.autonomy.proposals import LEARNED_KINDS, format_proposals
from k3code.autonomy.scope import SCOPES
from k3code.commands import CommandDef
from k3code.errors import ChainExhausted


def _msg(text: str, **extra: Any) -> dict[str, Any]:
    # "output" is what the TUI's slash handler shows for results it does not parse (type "message")
    return {"type": "message", "message": text, "output": text, **extra}


def _live(ctx: Any, session_id: str | None) -> Any:
    return ctx.sessions.get(session_id) if session_id else None


class ScopeCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="scope", help=f"Override the scope of the next task: /scope <{'|'.join(SCOPES)}|auto>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = _live(ctx, session_id)
        if live is None:
            return _msg("No active session.")
        if not arg:
            cur = live.scope_override or "auto (classified per task)"
            return _msg(f"Scope for the next task: {cur}. Usage: /scope <{'|'.join(SCOPES)}|auto>")
        level = arg.strip().lower()
        if level == "auto":
            live.scope_override = None
            return _msg("Scope override cleared; the next task is classified.")
        if level not in SCOPES:
            return _msg(f"Unknown scope: {level} ({'|'.join(SCOPES)}|auto)")
        live.scope_override = level
        if hasattr(ctx, "learning"):
            ctx.learning.record("scope", live, subject="override", choice=level)
        return _msg(f"The next task will be treated as '{level}' (danger checks still apply).")


class ProposalsCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="proposals", help="List proposals: /proposals [all|accept <id>|dismiss <id>]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        store = ctx.autonomy.proposals
        parts = arg.split()
        if parts[:1] in (["accept"], ["dismiss"]):
            if len(parts) < 2:
                return _msg(f"Usage: /proposals {parts[0]} <id>")
            p = store.get(parts[1])
            if p is None:
                return _msg(f"No such proposal: {parts[1]}")
            hub = getattr(ctx, "learning", None)
            live = _live(ctx, session_id)
            if parts[0] == "dismiss":
                store.set_status(p.id, "dismissed")
                if hub is not None:
                    hub.decide(p, "dismiss", live)
                return _msg(f"Dismissed {p.id}; it will not come back.")
            store.set_status(p.id, "accepted")
            if hub is not None:
                hub.decide(p, "accept", live)
                if p.kind in LEARNED_KINDS and p.payload.get("op") != "send":
                    return _msg(f"Accepted {p.id}: {await hub.apply(p, live)}")
            return {"type": "send", "message": p.action, "text": p.action, "notice": f"Accepted {p.id}: {p.text}"}
        items = store.all()
        if parts[:1] != ["all"]:
            items = [p for p in items if p.status == "pending"]
        return _msg(format_proposals(items))


class PreviewCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="preview", help="Fast sketch of a task's result, no changes: /preview <task>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = _live(ctx, session_id)
        if live is None:
            return _msg("No active session.")
        if not arg:
            return _msg("Usage: /preview <task>")
        if getattr(live, "streaming", False):  # the running turn's persist would overwrite the transcript rows
            return _msg("A turn is running in this session; /preview when it ends (or /stop it).")
        timeout = float(autonomy_cfg(ctx.config)["preview_timeout"])
        try:
            text = await preview_mod.preview(ctx.model_caller, arg, session_id=live.session_id, timeout=timeout)
        except TimeoutError:
            return _msg(f"Preview timed out after {timeout:.0f}s; try a shorter task.")
        except ChainExhausted as e:  # e.g. "all providers rate-limited until HH:MM"
            return _msg(f"Preview unavailable: {e}")
        live.stored.meta["preview_task"] = arg
        write_artifact_file(ctx, "preview", ctx._home() / "artifacts" / "preview", f"preview {arg}", text,
                            session=live.session_id)
        live.messages = [
            *live.messages,
            {"role": "user", "content": f"/preview {arg}"},
            {"role": "assistant", "content": f"[preview]\n{text}"},
        ]
        ctx.store.save(live.stored)
        live.emit("message.start", {})
        live.emit("message.delta", {"text": text})
        live.emit("message.complete", {"text": text, "tag": "preview", "status": "done", "state": live.state})
        return _msg("Preview ready. Run it for real with /go, or adjust the task.", tag="preview")


class GoCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="go", help="Run the last previewed task for real (scope gate applies)")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = _live(ctx, session_id)
        from k3code.commands.ultra_cmd import go_for_ultraplan

        if (planned := go_for_ultraplan(live)) is not None:
            ctx.store.save(live.stored)
            return planned
        task = (live.stored.meta.get("preview_task") if live else None) or ""
        if not task:
            return _msg("Nothing to run: use /preview <task> first.")
        return {"type": "send", "message": task, "text": task, "notice": f"Running for real: {task}"}


class AdvisorCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="advisor", help="Critical review from the strong tier: /advisor [question|accept]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = _live(ctx, session_id)
        if live is None:
            return _msg("No active session.")
        if arg.strip() == "accept":
            if not live.pending_advisor:
                return _msg("No advisor review waiting; run /advisor first.")
            if getattr(live, "streaming", False):
                # mid-turn the loop owns the transcript (its persist overwrites stored rows): steer it in instead
                live.steer_queue.append(f"Advisor review:\n{live.pending_advisor}")
                live.pending_advisor = ""
                return _msg("Advisor review added to the running turn.")
            live.messages = [*live.messages, {"role": "user", "content": f"Advisor review:\n{live.pending_advisor}"}]
            ctx.store.save(live.stored)
            live.pending_advisor = ""
            return _msg("Advisor review added to the conversation.")
        cfg = autonomy_cfg(ctx.config)
        context = await advisor.condensed_context(
            ctx.model_caller, live.stored.messages, threshold=int(cfg["advisor_compact_chars"]),
            session_id=live.session_id,
        )
        text = await advisor.advise(ctx.model_caller, context, arg, session_id=live.session_id)
        live.pending_advisor = text
        live.emit("advisor.show", {"session_id": live.session_id, "text": text, "question": arg})
        return _msg(text + "\n\n(side note: not in the conversation; /advisor accept to add it)", tag="advisor")
