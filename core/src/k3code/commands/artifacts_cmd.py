"""/artifacts: files that sessions produced. ``open <id>`` prints the path; ``publish <id> [--force]`` copies the file
into the published folder (``artifacts.publish_dir``, default ``<k3code home>/published``) and prints a file:// link,
plus the public link when ``artifacts.publish_url`` is a template such as ``https://example.com/{name}``.

Nothing is uploaded: the copy stays on this machine, and the public link is only printed for the user to host it."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from k3code.artifacts import AlreadyPublished, publish_file
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, split_args
from k3code.paths import home

PUBLISH_USAGE = "Usage: /artifacts publish <id> [--force]"


class ArtifactsCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="artifacts",
            help="List produced files: /artifacts [kind] | open <id> | publish <id> [--force]",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        store = ctx.artifacts
        parts = split_args(arg)
        if parts[:1] == ["open"]:
            art = store.get(parts[1]) if len(parts) > 1 else None
            if art is None:
                return reply("Usage: /artifacts open <id> (see /artifacts)")
            editor = os.environ.get("EDITOR", "")
            note = f" (open with: {editor} {art.path})" if editor else ""
            return reply(f"{art.path}{note}", path=art.path, editor=editor, id=art.id)
        if parts[:1] == ["publish"]:
            return _publish(ctx, store, parts[1:])
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


def _publish_dir(cfg: dict[str, Any]) -> Path:
    """``artifacts.publish_dir`` (``~`` expanded; a relative path goes under the k3code home), else ``published``."""
    raw = str(cfg.get("publish_dir") or "").strip()
    if not raw:
        return home() / "published"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else home() / path


def _publish(ctx: Any, store: Any, args: list[str]) -> dict[str, Any]:
    force = pop_flag(args, "--force")
    art = store.get(args[0]) if len(args) == 1 else None
    if art is None:
        return reply(PUBLISH_USAGE)
    if not art.exists:
        return reply(f"Cannot publish {art.id}: the file is gone ({art.path}).")
    cfg = getattr(getattr(ctx, "config", None), "artifacts", None) or {}
    try:
        dest = publish_file(Path(art.path), _publish_dir(cfg), force=force)
    except AlreadyPublished as e:
        return reply(f"Not published: {e} already exists. Run /artifacts publish {art.id} --force to replace it.")
    except OSError as e:
        return reply(f"Publish failed: {e}")
    file_url = dest.resolve().as_uri()
    lines = [f"Published {art.id} to {dest}", f"file: {file_url}"]
    public = ""
    template = str(cfg.get("publish_url") or "").strip()
    if template:
        try:
            public = template.format(name=quote(dest.name), id=quote(art.id))
        except (KeyError, IndexError, ValueError) as e:
            lines.append(f"public link skipped: artifacts.publish_url does not format ({e})")
        else:
            lines.append(f"public: {public}")
    return reply("\n".join(lines), path=str(dest), url=file_url, public_url=public, id=art.id)
