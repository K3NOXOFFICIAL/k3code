"""Helpers shared by command modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def reply(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "message", "message": text, "output": text, **extra}


class ArgError(ValueError):
    """A command line that cannot be read (a flag without its value): the message is shown to the user as it is."""


def split_args(arg: str) -> list[str]:
    """Words of a command line. ``"..."`` quotes anywhere (``\\"`` and ``\\\\`` escape inside); ``'...'`` only opens
    at the start of a word and closes before a space or the end, so an apostrophe inside a word (``don't``, ``Bob's``)
    is an ordinary character. A quote that never closes is an ordinary character too. (``shlex`` failed the whole line
    on one apostrophe, and the whitespace split it fell back on cut ``--check "pytest -x"`` in two.)"""
    words: list[str] = []
    cur: list[str] | None = None
    i, n = 0, len(arg)
    while i < n:
        ch = arg[i]
        if ch.isspace():
            if cur is not None:
                words.append("".join(cur))
                cur = None
            i += 1
            continue
        if cur is None:
            cur = []
        if ch == "\\" and i + 1 < n and arg[i + 1] in " \"'\\":
            cur.append(arg[i + 1])
            i += 2
        elif ch == '"':
            end, text = _double_quoted(arg, i)
            if end < 0:
                cur.append(ch)
                i += 1
            else:
                cur.append(text)
                i = end + 1
        elif ch == "'" and not cur:
            end = _single_quote_end(arg, i)
            if end < 0:
                cur.append(ch)
                i += 1
            else:
                cur.append(arg[i + 1 : end])
                i = end + 1
        else:
            cur.append(ch)
            i += 1
    if cur is not None:
        words.append("".join(cur))
    return words


def _double_quoted(arg: str, start: int) -> tuple[int, str]:
    """(index of the closing quote, text between) of the ``"`` at ``start``; (-1, "") when it never closes."""
    out: list[str] = []
    i = start + 1
    while i < len(arg):
        ch = arg[i]
        if ch == "\\" and i + 1 < len(arg) and arg[i + 1] in '"\\':
            out.append(arg[i + 1])
            i += 2
        elif ch == '"':
            return i, "".join(out)
        else:
            out.append(ch)
            i += 1
    return -1, ""


def _single_quote_end(arg: str, start: int) -> int:
    """Index of the ``'`` closing the one at ``start``: the next one followed by a space or the end; -1 if none."""
    i = arg.find("'", start + 1)
    while i >= 0:
        if i + 1 == len(arg) or arg[i + 1].isspace():
            return i
        i = arg.find("'", i + 1)
    return -1


def pop_flag(args: list[str], flag: str) -> bool:
    if flag in args:
        args.remove(flag)
        return True
    return False


def pop_option(args: list[str], flag: str) -> str | None:
    """Remove ``flag VALUE`` (or ``flag=VALUE``) from ``args``; returns VALUE (None when the flag is not there).

    A flag with no value (last word, or followed by another ``--flag``) raises :class:`ArgError`: it used to stay in
    the text, and ``/goal fix tests --turns`` made "--turns" part of the goal."""
    for i, a in enumerate(args):
        if a == flag:
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                raise ArgError(f"{flag} needs a value")
            val = args[i + 1]
            del args[i : i + 2]
            return val
        if a.startswith(flag + "="):
            val = a.split("=", 1)[1]
            if not val:
                raise ArgError(f"{flag} needs a value")
            del args[i]
            return val
    return None


def session_cwd(ctx: Any, session_id: str | None) -> Path:
    live = ctx.sessions.get(session_id) if session_id else None
    if live is not None and live.stored.cwd:
        return Path(live.stored.cwd)
    return Path.cwd()
