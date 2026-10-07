"""/export [path] [--all] [--session ID] [--settings-only] [--session-only]."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from k3code.artifacts import register_artifact
from k3code.bundle import BundleError, write_bundle
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, pop_option, reply, session_cwd, split_args


def run_export(
    store: Any,
    cwd: Path,
    *,
    path: str | None,
    all_sessions: bool = False,
    session: str | None = None,
    current: str | None = None,
    settings_only: bool = False,
    session_only: bool = False,
) -> tuple[Path, dict[str, Any]]:
    if all_sessions:
        ids = [s.session_id for s in store.list(limit=100_000)]
    elif session:
        ids = [session]
    elif current:
        ids = [current]
    else:
        ids = []
    if settings_only:
        ids = []
    out = Path(path).expanduser() if path else cwd / f"k3code-export-{time.strftime('%Y%m%d-%H%M%S')}.k3bundle"
    if not out.is_absolute():
        out = cwd / out
    manifest = write_bundle(out, store=store, cwd=cwd, session_ids=ids, settings=not session_only)
    return out, manifest


class ExportCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="export",
            help="Export settings (secrets redacted) and sessions: /export [path] [--all|--session ID] "
            "[--settings-only|--session-only]",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        all_ = pop_flag(args, "--all")
        settings_only = pop_flag(args, "--settings-only")
        session_only = pop_flag(args, "--session-only")
        sid = pop_option(args, "--session")
        try:
            out, manifest = run_export(
                ctx.store,
                session_cwd(ctx, session_id),
                path=args[0] if args else None,
                all_sessions=all_,
                session=sid,
                current=session_id,
                settings_only=settings_only,
                session_only=session_only,
            )
        except BundleError as e:
            return reply(f"Export failed: {e}")
        register_artifact(ctx, "export", out, title=out.name, session=session_id or "")
        c = manifest["contents"]
        return reply(
            f"Exported {len(c['sessions'])} session(s), {len(c['settings'])} settings file(s) to {out} "
            "(secrets redacted).",
            path=str(out),
            contents=c,
        )
