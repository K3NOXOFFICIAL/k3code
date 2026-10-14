"""/export [path] [--all] [--session ID] [--settings-only|--session-only|--memory-only] [--no-memory]."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from k3code.artifacts import register_artifact
from k3code.bundle import BundleError, describe_memory, write_bundle
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
    config: Any = None,
    memory_only: bool = False,
    no_memory: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Memory is exported unless ``no_memory``; each ``*_only`` exports that part alone."""
    only = [f for f, on in (("--settings-only", settings_only), ("--session-only", session_only)) if on]
    only += ["--memory-only"] if memory_only else []
    if len(only) > 1:
        raise BundleError(f"{' and '.join(only)} exclude each other (together they export nothing)")
    if memory_only and no_memory:
        raise BundleError("--memory-only and --no-memory exclude each other (together they export nothing)")
    if all_sessions:
        ids = [s.session_id for s in store.list(limit=100_000)]
    elif session:
        ids = [session]
    elif current:
        ids = [current]
    else:
        ids = []
    if settings_only or memory_only:
        ids = []
    out = Path(path).expanduser() if path else cwd / f"k3code-export-{time.strftime('%Y%m%d-%H%M%S')}.k3bundle"
    if not out.is_absolute():
        out = cwd / out
    manifest = write_bundle(
        out,
        store=store,
        cwd=cwd,
        session_ids=ids,
        settings=not (session_only or memory_only),
        config=config,
        memory=not (no_memory or settings_only or session_only),
    )
    return out, manifest


def export_summary(out: Path, manifest: dict[str, Any]) -> str:
    """The one line /export and `k3code export` print."""
    c = manifest["contents"]
    memory = describe_memory(manifest.get("memory_counts") or {})
    return (
        f"Exported {len(c['sessions'])} session(s), {len(c['settings'])} settings file(s), memory: {memory} to {out} "
        "(secrets redacted)."
    )


class ExportCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="export",
            help="Export settings (secrets redacted), sessions and memory: /export [path] [--all|--session ID] "
            "[--settings-only|--session-only|--memory-only] [--no-memory]",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        all_ = pop_flag(args, "--all")
        settings_only = pop_flag(args, "--settings-only")
        session_only = pop_flag(args, "--session-only")
        memory_only = pop_flag(args, "--memory-only")
        no_memory = pop_flag(args, "--no-memory")
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
                config=ctx.config,
                memory_only=memory_only,
                no_memory=no_memory,
            )
        except BundleError as e:
            return reply(f"Export failed: {e}")
        register_artifact(ctx, "export", out, title=out.name, session=session_id or "")
        return reply(export_summary(out, manifest), path=str(out), contents=manifest["contents"])
