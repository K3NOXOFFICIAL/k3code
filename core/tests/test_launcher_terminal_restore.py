"""When the TUI process dies without running its own exit handler (OOM kill, SIGKILL, crash), the terminal is put back
(#153): alternate screen, mouse tracking, bracketed paste and the cursor are the TUI's to restore, and a killed TUI
cannot. A signal death is reported as 128 + signo, not as ``sys.exit(-9)`` (= 247).

The launcher does not wait for the TUI itself (#164): it imports most of the gateway before it starts the TUI, and
that Python process stayed resident (about 47 MB) for the whole session doing nothing but waiting. It now execs into
a small ``sh`` waiter (:data:`cli._TUI_WAITER`) that runs the TUI, writes the reset after an abnormal exit when
stdout is a terminal, and exits with the TUI's status. These tests run that waiter for real, on a pty."""

from __future__ import annotations

import codecs
import os
import pty
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from k3code import cli as cli_mod

# What a TUI leaves armed in the terminal when it is killed mid-session.
_KILL_SELF = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"


class _Exec(Exception):
    """Raised by the stand-in for os.execve: the launcher handed its process over."""

    def __init__(self, path: str, argv: list[str], env: dict[str, str]) -> None:
        super().__init__(path)
        self.path, self.argv, self.env = path, argv, env


def _launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: str) -> _Exec:
    """Run ``_launch_tui`` with ``script`` (Python) as ``tui/dist/entry.js`` and capture the exec it ends in."""
    entry = tmp_path / "tui" / "dist" / "entry.js"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(script)
    import k3code.paths as paths_mod

    monkeypatch.setattr(paths_mod, "find_node", lambda: sys.executable)
    monkeypatch.setattr(cli_mod, "_find_repo_root", lambda: tmp_path)

    def fake_execve(path, argv, env):
        raise _Exec(path, list(argv), dict(env))

    def waited(*_a, **_k):
        pytest.fail("the launcher ran the TUI and waited on it instead of handing its process over")

    with monkeypatch.context() as m:
        m.setattr(os, "execve", fake_execve)
        m.setattr(subprocess, "run", waited)
        with pytest.raises(_Exec) as handed:
            cli_mod._launch_tui()
    return handed.value


def _run_waiter(handed: _Exec, *, tty: bool = True) -> tuple[int, str]:
    """Run the waiter the launcher exec'd into, with stdout on a pty (a terminal) or a pipe; its status and output."""
    if not tty:
        proc = subprocess.run(handed.argv, executable=handed.path, env=handed.env, stdout=subprocess.PIPE, check=False)
        return proc.returncode, proc.stdout.decode()
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(handed.argv, executable=handed.path, env=handed.env, stdout=slave)
        os.close(slave)
        out = b""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: the waiter closed its end
                break
            if not chunk:
                break
            out += chunk
        return proc.wait(timeout=60), out.decode()
    finally:
        os.close(master)


def test_the_launcher_hands_its_process_to_the_tui_waiter(tmp_path, monkeypatch):
    """#164: no Python launcher stays resident while the TUI runs; the TUI's command line and environment go to the
    waiter unchanged."""
    handed = _launch(monkeypatch, tmp_path, "")
    assert Path(handed.path).name == "sh" and handed.argv[1] == "-c" and handed.argv[2] == cli_mod._TUI_WAITER
    assert handed.argv[-2:] == [sys.executable, str(tmp_path / "tui" / "dist" / "entry.js")]
    assert handed.env["K3CODE_GATEWAY_CMD"].endswith("-m k3code.cli gateway --stdio")


def _planted_sh(where: Path) -> Path:
    """An executable ``sh`` in ``where`` (a project directory a relative PATH entry would find)."""
    where.mkdir(parents=True, exist_ok=True)
    sh = where / "sh"
    sh.write_text("#!/bin/sh\necho planted\n")
    sh.chmod(0o755)
    return sh


@pytest.mark.parametrize("path", [":/usr/bin:/bin", ".:/usr/bin:/bin", "/nonexistent::/usr/bin"])
def test_the_waiter_shell_is_never_found_through_a_relative_path_entry(tmp_path, monkeypatch, path):
    """An empty or ``.`` entry in PATH is the current directory (the project): ``shutil.which("sh")`` picked a planted
    ``./sh`` there, which then got the TUI's environment. The waiter is ``/bin/sh``."""
    project = tmp_path / "proj"
    _planted_sh(project)
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", path)
    handed = _launch(monkeypatch, tmp_path, "")
    assert handed.path == "/bin/sh" and handed.argv[0] == "/bin/sh"


def test_without_bin_sh_the_waiter_comes_from_an_absolute_path_entry_only(tmp_path, monkeypatch):
    project, bindir = tmp_path / "proj", tmp_path / "bin"
    _planted_sh(project)
    real = _planted_sh(bindir)
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli_mod, "_WAITER_SH", str(tmp_path / "no" / "sh"))
    assert cli_mod._waiter_shell(f".::{bindir}") == str(real)
    assert cli_mod._waiter_shell(".:") == str(tmp_path / "no" / "sh")  # nothing absolute: never ./sh


def test_killed_tui_gets_the_terminal_put_back(tmp_path, monkeypatch):
    rc, out = _run_waiter(_launch(monkeypatch, tmp_path, _KILL_SELF))
    for mode in (1000, 1002, 1003, 1004, 1006, 2004):
        assert f"\x1b[?{mode}l" in out, f"mode {mode} not disabled"
    assert "\x1b[?1049l" in out  # leaves the alternate screen
    assert out.endswith("\x1b[?25h")  # cursor visible, last so nothing hides it again


def test_killed_tui_exits_128_plus_the_signal(tmp_path, monkeypatch):
    rc, _ = _run_waiter(_launch(monkeypatch, tmp_path, _KILL_SELF))
    assert rc == 128 + signal.SIGKILL


def test_crashed_tui_gets_the_terminal_put_back_and_keeps_its_status(tmp_path, monkeypatch):
    rc, out = _run_waiter(_launch(monkeypatch, tmp_path, "raise SystemExit(3)\n"))
    assert rc == 3
    assert "\x1b[?1049l" in out and out.endswith("\x1b[?25h")


def test_clean_exit_leaves_the_terminal_alone(tmp_path, monkeypatch):
    rc, out = _run_waiter(_launch(monkeypatch, tmp_path, "print('bye', end='')\n"))
    assert rc == 0
    assert out == "bye"


def test_nothing_is_written_when_stdout_is_not_a_terminal(tmp_path, monkeypatch):
    rc, out = _run_waiter(_launch(monkeypatch, tmp_path, _KILL_SELF), tty=False)
    assert rc == 128 + signal.SIGKILL
    assert out == ""


def test_a_closed_stdout_does_not_hide_the_exit_status(tmp_path, monkeypatch):
    handed = _launch(monkeypatch, tmp_path, "raise SystemExit(3)\n")
    proc = subprocess.run(handed.argv, executable=handed.path, env=handed.env, stdout=subprocess.DEVNULL, check=False)
    assert proc.returncode == 3


def test_an_interrupt_waits_for_the_tui(tmp_path, monkeypatch):
    """Ctrl+C before the TUI has its terminal in raw mode reaches the whole process group: the waiter outlives it, so
    the TUI's own exit (and the reset after an abnormal one) still comes from the waiter."""
    script = (
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(5))\n"
        "os.killpg(0, signal.SIGINT)\n"
        "time.sleep(5)\n"
    )
    handed = _launch(monkeypatch, tmp_path, script)
    proc = subprocess.run(
        handed.argv, executable=handed.path, env=handed.env, start_new_session=True, check=False, timeout=60
    )
    assert proc.returncode == 5


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
