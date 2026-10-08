"""/artifacts: files that sessions produced. ``open <id>`` prints the path; ``publish`` is a stub."""

from __future__ import annotations

import os
import time
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply


class ArtifactsCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="artifacts",
            help="List produced files: /artifacts [kind] | open <id> | publish <id>",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        store = ctx.artifacts
        parts = arg.split()
        if parts[:1] == ["open"]:
            art = store.get(parts[1]) if len(parts) > 1 else None
            if art is None:
                return reply("Usage: /artifacts open <id> (see /artifacts)")
            editor = os.environ.get("EDITOR", "")
            note = f" (open with: {editor} {art.path})" if editor else ""
            return reply(f"{art.path}{note}", path=art.path, editor=editor, id=art.id)
        if parts[:1] == ["publish"]:
            art = store.get(parts[1]) if len(parts) > 1 else None
            if art is None:
                return reply("Usage: /artifacts publish <id>")
            # TODO(M6): publish to a cloud drive / a gist; needs a destination config and a confirmation step.
            return reply(f"Publishing is not implemented yet ({art.id}: {art.path}).")
        kind = parts[0] if parts else None
        rows = store.list(kind=kind)
        if not rows:
            return reply("No artifacts yet." if not kind else f"No '{kind}' artifacts.")
        lines = ["id        when        kind      title"]
        for a in rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(a.ts))
            lines.append(f"{a.id}  {when}  {a.kind:<9} {a.title[:60]}{'' if a.exists else '  (file missing)'}")
            lines.append(f"          {a.path}")
        return reply("\n".join(lines), artifacts=[a.__dict__ for a in rows])
