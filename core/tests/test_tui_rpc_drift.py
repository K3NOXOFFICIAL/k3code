"""The TUI calls the core by method name; a name the core does not handle is an "unknown method" error at runtime that
no test of either side sees. This test reads every method name out of the TUI sources and compares it with the
gateway's handler table."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from k3code.gateway.server import _HANDLERS

TUI_SRC = Path(__file__).resolve().parents[2] / "tui" / "src"

#: Methods the TUI names that the core does not implement, with the reason each is safe to leave. Remove an entry
#: when the core gains the handler (the stale-entry test below fails until it is removed).
UNREACHABLE = {
    "skills.manage": "SkillsHub overlay: nothing in the k3code TUI sets overlay.skillsHub (/skills prints a list)",
    "plugins.manage": "PluginsHub overlay: k3code has no plugin system and nothing sets overlay.pluginsHub",
    "learning.frames": "Journey overlay: nothing sets overlay.journey; the core's learning log has no frame renderer",
    "learning.detail": "Journey overlay (see learning.frames)",
    "learning.edit": "Journey overlay (see learning.frames)",
    "learning.delete": "Journey overlay (see learning.frames)",
    "clarify.lock": "batch clarify: the core's clarify request carries one question, never `questions`",
}

CALL = re.compile(
    r"""(?:\.rpc|\.request|quietRpc|rpcQuiet|\brpc|\brequest)\s*(?:<[^>()]*(?:<[^>]*>)?[^>()]*>)?\s*"""
    r"""\(\s*["'`]([a-z_]+(?:\.[a-z_]+)+)["'`]"""
)


def tui_methods() -> dict[str, str]:
    """Method name -> one file that calls it, for every non-test TUI source."""
    found: dict[str, str] = {}
    for path in [*TUI_SRC.rglob("*.ts"), *TUI_SRC.rglob("*.tsx")]:
        if "__tests__" in path.parts or ".test." in path.name:
            continue
        for match in CALL.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1), str(path.relative_to(TUI_SRC)))
    return found


pytestmark = pytest.mark.skipif(not TUI_SRC.is_dir(), reason="the TUI sources are not next to the core")


def test_the_scan_sees_the_tui_calls():
    called = tui_methods()
    assert len(called) > 40 and {"prompt.submit", "session.create", "config.get"} <= set(called)


def test_every_method_the_tui_calls_is_handled_or_explained():
    missing = {m: f for m, f in tui_methods().items() if m not in _HANDLERS and m not in UNREACHABLE}
    assert not missing, (
        f"the TUI calls methods the core has no handler for (add one, or list it in UNREACHABLE): {missing}"
    )


def test_the_allowlist_has_no_stale_entries():
    called = tui_methods()
    stale = [m for m in UNREACHABLE if m in _HANDLERS or m not in called]
    assert not stale, f"drop these from UNREACHABLE (handled now, or no longer called): {stale}"
