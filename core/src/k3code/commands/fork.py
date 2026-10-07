"""/fork [title] [--activate]: copy the current session into a new one."""

from __future__ import annotations

import copy
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, split_args
from k3code.gateway.sessions import StoredSession


def fork_session(store: Any, src: StoredSession, title: str = "", *, cwd: str | None = None) -> StoredSession:
    meta = copy.deepcopy(src.meta)
    meta["forked_from"] = src.session_id
    new = StoredSession(
        session_id=store.new_id(),
        title=title or (f"{src.title} (fork)" if src.title else "fork"),
        model=src.model,
        provider=src.provider,
        cwd=cwd or src.cwd,
        messages=copy.deepcopy(src.messages),
        usage={},
        meta=meta,
    )
    return store.insert(new)


class ForkCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="fork", help="Copy this session into a new one: /fork [title] [--activate]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        activate = pop_flag(args, "--activate")
        live = ctx.sessions.get(session_id) if session_id else None
        if live is None:
            return reply("No active session to fork.")
        ctx.store.save(live.stored)
        new = fork_session(ctx.store, live.stored, " ".join(args))
        if activate:
            ctx.activate_session(new.session_id)
        return reply(
            f"Forked → {new.session_id} ({len(new.messages)} messages)" + (" and activated." if activate else "."),
            session_id=new.session_id,
            activated=activate,
        )
