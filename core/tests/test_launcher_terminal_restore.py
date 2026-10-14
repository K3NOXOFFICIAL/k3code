"""When the TUI process dies without running its own exit handler (OOM kill, SIGKILL, crash), the launcher puts the
terminal back (#153): alternate screen, mouse tracking, bracketed paste and the cursor are the TUI's to restore, and a
killed TUI cannot. The launcher also reports a signal death as 128 + signo, not as ``sys.exit(-9)`` (= 247)."""

from __future__ import annotations

import codecs
import re
import signal
import sys
from pathlib import Path

import pytest

from k3code import cli as cli_mod


class _Tty:
    """A stdout that says it is a terminal and keeps what the launcher writes to it."""

    def __init__(self, *, tty: bool = True) -> None:
        self.tty = tty
        self.written: list[str] = []
        self.flushes = 0

    def isatty(self) -> bool:
        return self.tty

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1

    @property
    def text(self) -> str:
        return "".join(self.written)


def _fake_tui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: str) -> None:
    """Run ``script`` (Python) in place of ``node tui/dist/entry.js``: a real child process, a real exit status."""
    entry = tmp_path / "tui" / "dist" / "entry.js"
    entry.parent.mkdir(parents=True)
    entry.write_text(script)
    import k3code.paths as paths_mod

    monkeypatch.setattr(paths_mod, "find_node", lambda: sys.executable)
    monkeypatch.setattr(cli_mod, "_find_repo_root", lambda: tmp_path)


def _launch(tty: _Tty, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "stdout", tty)
    with pytest.raises(SystemExit) as exit_info:
        cli_mod._launch_tui()
    code = exit_info.value.code
    assert isinstance(code, int)
    return code


# What a TUI leaves armed in the terminal when it is killed mid-session.
_KILL_SELF = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"


def test_killed_tui_gets_the_terminal_put_back(tmp_path, monkeypatch):
    _fake_tui(monkeypatch, tmp_path, _KILL_SELF)
    tty = _Tty()
    _launch(tty, monkeypatch)
    out = tty.text
    for mode in (1000, 1002, 1003, 1004, 1006, 2004):
        assert f"\x1b[?{mode}l" in out, f"mode {mode} not disabled"
    assert "\x1b[?1049l" in out  # leaves the alternate screen
    assert out.endswith("\x1b[?25h")  # cursor visible, last so nothing hides it again
    assert tty.flushes


def test_killed_tui_exits_128_plus_the_signal(tmp_path, monkeypatch):
    _fake_tui(monkeypatch, tmp_path, _KILL_SELF)
    assert _launch(_Tty(), monkeypatch) == 128 + signal.SIGKILL


def test_crashed_tui_gets_the_terminal_put_back_and_keeps_its_status(tmp_path, monkeypatch):
    _fake_tui(monkeypatch, tmp_path, "raise SystemExit(3)\n")
    tty = _Tty()
    assert _launch(tty, monkeypatch) == 3
    assert "\x1b[?1049l" in tty.text and tty.text.endswith("\x1b[?25h")


def test_clean_exit_leaves_the_terminal_alone(tmp_path, monkeypatch):
    _fake_tui(monkeypatch, tmp_path, "")
    tty = _Tty()
    assert _launch(tty, monkeypatch) == 0
    assert tty.text == ""


def test_nothing_is_written_when_stdout_is_not_a_terminal(tmp_path, monkeypatch):
    _fake_tui(monkeypatch, tmp_path, _KILL_SELF)
    tty = _Tty(tty=False)
    assert _launch(tty, monkeypatch) == 128 + signal.SIGKILL
    assert tty.text == ""


def test_a_closed_stdout_does_not_hide_the_exit_status(tmp_path, monkeypatch):
    _fake_tui(monkeypatch, tmp_path, "raise SystemExit(3)\n")

    class _Closed(_Tty):
        def write(self, text: str) -> int:
            raise ValueError("I/O operation on closed file")

    assert _launch(_Closed(), monkeypatch) == 3


def test_reset_block_matches_the_tui(monkeypatch):
    """The launcher repeats the TUI's reset (``TERMINAL_MODE_RESET``); a mode added there must be added here."""
    ts = Path(__file__).resolve().parents[2] / "tui" / "src" / "lib" / "terminalModes.ts"
    if not ts.is_file():
        pytest.skip("tui sources not in this layout")
    block = re.search(r"TERMINAL_MODE_RESET =(.*?);", ts.read_text(), re.S)
    assert block, "TERMINAL_MODE_RESET not found in terminalModes.ts"
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', block.group(1))
    tui_reset = "".join(codecs.decode(s, "unicode_escape") for s in literals)
    assert tui_reset == cli_mod._TERMINAL_RESET
