"""Helpers shared by command modules."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


def reply(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "message", "message": text, "output": text, **extra}


def split_args(arg: str) -> list[str]:
    try:
        return shlex.split(arg)
    except ValueError:
        return arg.split()


def pop_flag(args: list[str], flag: str) -> bool:
    if flag in args:
        args.remove(flag)
        return True
    return False


def pop_option(args: list[str], flag: str) -> str | None:
    """Remove ``flag VALUE`` (or ``flag=VALUE``) from ``args``; returns VALUE."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            val = args[i + 1]
            del args[i : i + 2]
            return val
        if a.startswith(flag + "="):
            del args[i]
            return a.split("=", 1)[1]
    return None


def session_cwd(ctx: Any, session_id: str | None) -> Path:
    live = ctx.sessions.get(session_id) if session_id else None
    if live is not None and live.stored.cwd:
        return Path(live.stored.cwd)
    return Path.cwd()
