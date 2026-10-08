"""/branch [name] [--worktree] [--activate]: git branch (or worktree) + forked session, linked."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, split_args
from k3code.commands.fork import fork_session
from k3code.memory import project_root

_NAME_OK = re.compile(r"^[A-Za-z0-9._/-]+$")


async def _git(cwd: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


class BranchCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="branch",
            help="Create a git branch and fork the session onto it: /branch [name] [--worktree] [--activate]",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        worktree = pop_flag(args, "--worktree")
        activate = pop_flag(args, "--activate")
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return reply("No active session to branch.")
        cwd = Path(live.stored.cwd or Path.cwd())
        rc, out = await _git(cwd, "rev-parse", "--is-inside-work-tree")
        if rc != 0 or out != "true":
            return reply(f"Not a git repository: {cwd}. /branch needs the session cwd to be inside a git repo.")
        name = args[0] if args else f"k3/{time.strftime('%Y%m%d-%H%M%S')}"
        if not _NAME_OK.match(name) or name.startswith("-"):
            return reply(f"Invalid branch name: {name!r}")
        rc, top = await _git(cwd, "rev-parse", "--show-toplevel")
        root = Path(top) if rc == 0 else project_root(cwd)
        new_cwd = str(cwd)
        if worktree:
            wt = root / ".k3code" / "worktrees" / name.replace("/", "-")
            wt.parent.mkdir(parents=True, exist_ok=True)
            rc, out = await _git(root, "worktree", "add", "-b", name, str(wt))
            if rc != 0:
                return reply(f"git worktree add failed: {out}")
            new_cwd = str(wt)
        else:
            rc, out = await _git(cwd, "switch", "-c", name)
            if rc != 0:
                return reply(f"git switch -c failed: {out}")
        ctx.store.save(live.stored)
        new = fork_session(ctx.store, live.stored, f"branch: {name}", cwd=new_cwd)
        new.meta.update({"branch": name, "worktree": new_cwd if worktree else None, "branched_from": live.session_id})
        ctx.store.save(new)
        live.stored.meta.setdefault("branches", []).append({"name": name, "session_id": new.session_id})
        ctx.store.save(live.stored)
        if activate:
            ctx.activate_session(new.session_id)
        where = f"worktree {new_cwd}" if worktree else f"branch {name} (cwd {new_cwd})"
        return reply(
            f"Created {where}; forked session {new.session_id}.",
            session_id=new.session_id,
            branch=name,
            cwd=new_cwd,
            activated=activate,
        )
