"""/bugs [--json] [--since 7d] | show <fp> | export [path] [--since 7d] | resolve <fp>|--all."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from k3code import bugread
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, pop_option, reply, session_cwd, split_args

DEFAULT_SINCE = "7d"
_SINCE = re.compile(r"(\d+(?:\.\d+)?)d?")
_USAGE = "Usage: /bugs [--json] [--since 7d] | show <fp> | export [path] [--since 7d] | resolve <fp>|--all"


def parse_since(text: str) -> float:
    """Days in ``7d`` or a bare ``7``."""
    m = _SINCE.fullmatch(text.strip())
    if not m:
        raise ValueError(f"--since wants a number of days such as 7d, not {text!r}")
    return float(m.group(1))


def open_groups(since_days: float) -> list[bugread.Group]:
    """The open groups seen in the last ``since_days`` days, newest first."""
    cutoff = time.time() - since_days * bugread._DAY
    return [g for g in bugread.groups() if g.last_seen >= cutoff]


def format_groups(groups: list[bugread.Group], since_days: float) -> str:
    if not groups:
        return f"No open bug groups in the last {since_days:g} day(s)."
    lines = [f"{len(groups)} open bug group(s) in the last {since_days:g} day(s):"]
    for g in groups:
        first = g.summary.splitlines()[0][:100] if g.summary else ""
        lines.append(f"{g.fp}  {g.kind}  x{g.count}  {bugread._when(g.last_seen)}  {first}")
    return "\n".join(lines)


def groups_json(groups: list[bugread.Group]) -> str:
    return json.dumps([asdict(g) for g in groups], indent=2, default=str)


def show_json(fp: str) -> str | None:
    """The latest record of group ``fp`` as JSON, or None for an unknown group."""
    record = bugread.show(fp)
    return None if record is None else json.dumps(record, indent=2, default=str)


def resolve_message(fp: str | None, every: bool) -> tuple[bool, str]:
    """Resolve ``fp``, or every open group when ``every``; ``(ok, message)``."""
    marked = bugread.resolve("all" if every else str(fp))
    if marked:
        return True, f"Resolved {len(marked)} bug group(s): {', '.join(marked)}"
    return every, "No open bug groups." if every else f"Unknown bug group: {fp}"


class BugsCommand(CommandDef):
    headless = True
    needs_server = False

    def __init__(self) -> None:
        super().__init__(name="bugs", help="Captured k3code bugs: " + _USAGE.removeprefix("Usage: "))

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        as_json = pop_flag(args, "--json")
        every = pop_flag(args, "--all")
        try:
            since = parse_since(pop_option(args, "--since") or DEFAULT_SINCE)
        except ValueError as e:
            return reply(str(e))
        sub, rest = (args[0], args[1:]) if args else ("", [])
        if sub == "show" and len(rest) == 1:
            text = show_json(rest[0])
            return reply(text or f"Unknown bug group: {rest[0]}")
        if sub == "resolve" and (len(rest) == 1) != every:
            _ok, text = resolve_message(rest[0] if rest else None, every)
            return reply(text)
        if sub == "export" and len(rest) <= 1:
            path = Path(rest[0]).expanduser() if rest else None
            if path is not None and not path.is_absolute():
                path = session_cwd(ctx, session_id) / path
            out = bugread.export(path, since)
            return reply(f"Wrote the bug brief to {out}", path=str(out))
        if sub:
            return reply(_USAGE)
        found = open_groups(since)
        return reply(groups_json(found) if as_json else format_groups(found, since), fps=[g.fp for g in found])
