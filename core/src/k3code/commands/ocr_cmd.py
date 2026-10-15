"""/ocr <path>: the text of an image or PDF through the offline OCR (k3code.ocr), printed as is."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from k3code import ocr
from k3code.commands import CommandDef
from k3code.commands._util import pop_option, reply, session_cwd, split_args

USAGE = "Usage: /ocr [--pages N] <path>  (an image: png, jpg, webp, gif, bmp, tiff; or a PDF)"


class OcrCommand(CommandDef):
    headless = True
    needs_server = False

    def __init__(self) -> None:
        super().__init__(name="ocr", help="Text of an image or scanned PDF, offline: /ocr [--pages N] <path>")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg) if arg else []
        pages = pop_option(args, "--pages")
        if len(args) != 1:
            return reply(USAGE)
        max_pages = ocr.DEFAULT_MAX_PAGES
        if pages is not None:
            if not pages.isdigit() or int(pages) < 1:
                return reply(USAGE)
            max_pages = int(pages)
        cwd = session_cwd(ctx, session_id) if hasattr(ctx, "sessions") else Path.cwd()
        path = Path(args[0]).expanduser()
        if not path.is_absolute():
            path = (cwd / path).resolve()
        if not path.is_file():
            return reply(f"File not found: {path}")
        if (why := _refused(ctx, session_id, path)) is not None:
            return reply(why)
        try:
            result = await asyncio.to_thread(ocr.ocr_file, path, max_pages)
        except ocr.OcrUnavailable as exc:
            return reply(f"/ocr cannot run: {exc}")
        return reply(result.render(), engine=result.engine, confidence=result.confidence)


def _refused(ctx: Any, session_id: str | None, path: Path) -> str | None:
    """Reading a file for OCR is a ``read``: the session's permission engine decides it with the same rules. The
    person typed the path, so an ``ask`` is their own approval; a deny (a credential path, a plan-mode rule) holds."""
    live = ctx.sessions.get(session_id) if session_id and hasattr(ctx, "sessions") else None
    perms = getattr(live, "perms", None)
    if perms is None:
        from k3code.permissions.engine import sensitive_path

        return f"Refused: {path} is a credential location" if sensitive_path(str(path)) else None
    decision = perms.decide("read", {"path": str(path)})
    if decision.action == "deny":
        return f"Refused: {decision.message or path}"
    return None
