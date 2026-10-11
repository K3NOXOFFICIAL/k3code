"""Writes for the files learning keeps for the user (USER.md, gotchas, preferences, experiments, state).

``Path.write_text`` truncates the file and only then encodes, so text with a lone surrogate (a cheap-model reply
with ``\\ud83d``, a non-UTF-8 file name decoded by ``os.fsdecode``) raised ``UnicodeEncodeError`` and left the file
empty. :func:`write_text_atomic` makes the text encodable first and replaces the file in one step.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
from pathlib import Path


def encodable(text: str) -> str:
    """``text`` with every lone surrogate replaced by ``?``: UTF-8 cannot encode them (sqlite and files refuse)."""
    return text.encode("utf-8", "replace").decode("utf-8")


def write_text_atomic(path: Path, text: str, *, private: bool = False) -> None:
    """Write ``text`` to ``path`` through a temp file in the same directory and ``os.replace``; a failure (or a crash)
    leaves the old file as it was. ``private``: the file is 0600 whatever the umask (an existing one keeps its mode
    otherwise). A symlinked file stays a link: the file it points to is replaced."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(encodable(text))
        if not private and target.exists():
            with contextlib.suppress(OSError):
                shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
