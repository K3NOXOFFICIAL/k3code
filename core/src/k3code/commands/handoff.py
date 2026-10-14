"""/handoff [--session ID] [--out DIR] [--embed] [--tail N] [--ai]: a paste-able text that continues the session in any
harness, plus a .k3bundle (see k3code.handoff)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from k3code.artifacts import register_artifact
from k3code.bundle import BundleError
from k3code.commands import CommandDef
from k3code.commands._util import ArgError, pop_flag, pop_option, reply, session_cwd, split_args
from k3code.gateway.sessions import SessionStore
from k3code.handoff import DEFAULT_TAIL, BuiltHandoff, HandoffError, ai_narrative, build, notice

logger = logging.getLogger(__name__)

TAIL_USAGE = "--tail needs a whole number of messages (0 or more)"


class HandoffCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="handoff",
            help="Copy a text that continues this session in another harness or on another device (plus a .k3bundle):"
            " /handoff [--session ID] [--out DIR] [--embed] [--tail N] [--ai]",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        try:
            embed = pop_flag(args, "--embed")
            ai = pop_flag(args, "--ai")
            sid = pop_option(args, "--session") or session_id
            out = pop_option(args, "--out")
            tail_arg = pop_option(args, "--tail")
            if args:
                raise ArgError(f"unexpected argument: {args[0]}")
            if tail_arg is not None and not re.fullmatch(r"[0-9]+", tail_arg):
                raise ArgError(TAIL_USAGE)
            tail = int(tail_arg) if tail_arg is not None else DEFAULT_TAIL
        except ArgError as e:
            return reply(f"Handoff failed: {e}")
        if not sid:
            return reply("Handoff failed: no session (start one, or pass --session ID)")
        stored = ctx.store.get(sid)
        if stored is None:
            return reply(f"Handoff failed: unknown session: {sid}")
        narrative, ai_failed = None, False
        if ai:
            try:
                narrative = await ai_narrative(getattr(ctx, "model_caller", None), stored.messages, session_id=sid)
            except Exception:  # noqa: BLE001 - any failure falls back to the deterministic text
                logger.warning("handoff summary failed", exc_info=True)
                ai_failed = True
        usage = getattr(ctx, "usage", None)  # a sqlite connection of the loop thread: read here, not in the worker
        try:
            rows = usage.rows(session=sid) if usage is not None else []
        except Exception:  # noqa: BLE001 - the route then lists the session's model alone
            rows = []
        cwd, store_path, config = session_cwd(ctx, sid), ctx.store.path, ctx.config

        def run() -> BuiltHandoff:  # git, the bundle, the config: seconds that must not stall the event loop
            store = SessionStore(store_path)  # ctx.store's connection belongs to the loop thread
            try:
                return build(
                    store,
                    sid,
                    cwd=cwd,
                    out_dir=out,
                    embed=embed,
                    tail=tail,
                    narrative=narrative,
                    config=config,
                    usage_rows=rows,
                    ai_failed=ai_failed,
                )
            finally:
                store.close()

        try:
            built = await asyncio.to_thread(run)
        except (HandoffError, BundleError, OSError) as e:
            return reply(f"Handoff failed: {e}")
        register_artifact(ctx, "handoff", built.text_path, title=built.text_path.name, session=sid)
        register_artifact(ctx, "handoff", built.bundle_path, title=built.bundle_path.name, session=sid)
        return reply(
            built.text,
            path=str(built.text_path),
            bundle=str(built.bundle_path),
            clipboard=built.text,
            notice=notice(built),
        )
