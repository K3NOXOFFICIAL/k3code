"""/help lists each command exactly once, on one line (multi-line usage must not look like a duplicate)."""

from __future__ import annotations

import asyncio

from k3code.commands.builtin import build_registry


def test_help_has_no_duplicates() -> None:
    reg = build_registry()

    class Ctx:
        commands = reg

    out = asyncio.run(reg.dispatch(Ctx(), "help", "", None))
    lines = out["message"].splitlines()[1:]
    assert all(line.startswith("/") for line in lines), lines
    names = [line.split(" ", 1)[0] for line in lines]
    assert len(names) == len(set(names))
    assert len(names) == len(reg.names())
