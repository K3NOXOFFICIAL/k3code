"""/project [rescan]: the project's detected stacks, facts, scan age and pending recipe proposals."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply, session_cwd, split_args
from k3code.learning import projectprep, projectstate, recipes

USAGE = "Usage: /project | /project rescan"


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{max(seconds, 0):.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


class ProjectCommand(CommandDef):
    headless = True

    def __init__(self) -> None:
        super().__init__(
            name="project",
            help="Detected stacks, project facts and pending recipe proposals; /project rescan re-scans now",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        if args not in ([], ["rescan"]):
            return reply(USAGE)
        cwd = session_cwd(ctx, session_id)
        root = projectstate.project_root(cwd)
        store = ctx.autonomy.proposals
        note = ""
        if args == ["rescan"]:
            made = await projectprep.prepare(
                root, store=store, skill_roots=list(ctx.config.skills.roots), session_id=session_id or ""
            )
            live = ctx.sessions.get(session_id) if session_id else None
            if live is not None and made and getattr(ctx, "learning", None) is not None:
                ctx.learning.emit(live, made)
            note = f"Re-scanned: {len(made)} new proposal(s).\n" if made else "Re-scanned: nothing changed.\n"
        state = projectstate.load(root)
        found = state.get("stacks") or []
        if not found:
            return reply(note + f"No stacks detected in {root} (yet). /project rescan scans now.", stacks=[])
        changed = await asyncio.to_thread(projectprep.needs_prep, root)
        age = _age(time.time() - float(state.get("detected_at") or 0))
        lines = [
            f"Project {root}",
            f"Scanned {age} ago" + (" (files changed since: /project rescan)" if changed else ""),
        ]
        facts = projectprep.facts_prompt(root)
        lines += [ln for ln in facts.splitlines() if ln and not ln.startswith(("```", "## ", "Detected from"))]
        pending = projectprep.pending_recipes(store, root)
        if pending:
            lines.append("Pending recipe proposals (/proposals accept <id> | dismiss <id>):")
            lines += [f"  {p.id} [{p.kind}] {p.text}" for p in pending]
        else:
            lines.append("No pending recipe proposals.")
        return reply(
            note + "\n".join(lines),
            stacks=[{"id": s["id"], "dir": s["dir"], "label": recipes.label(s)} for s in found],
            pending=[p.id for p in pending],
            changed=changed,
        )
