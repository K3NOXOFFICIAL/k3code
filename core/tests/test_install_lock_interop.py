"""install.sh and `k3code update` share one `.install.lock`; each honours (and takes over) the other's (#31, #43).

Each side has tests against its own writer. These start the real install.sh and let `k3code update` look at the lock
it wrote, so a change to the format on one side (pid, start time) breaks a test instead of an install.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from env_fixtures import hide_root  # noqa: F401  (a fixture)
from k3code import update as upd

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "install" / "install.sh"
DATA_REL = Path(".local") / "share" / "k3code"

pytestmark = [
    pytest.mark.skipif(shutil.which("uv") is None, reason="installer needs uv present"),
    pytest.mark.usefixtures("hide_root"),
]


def _hold_install_sh(tmp_path: Path) -> tuple[subprocess.Popen[bytes], Path]:
    """Start install.sh and return once it holds its lock; a stub git keeps it there until the test kills it."""
    stubs = tmp_path / "stubbin"
    stubs.mkdir()
    git = stubs / "git"
    git.write_text(f"#!/bin/sh\n: >{tmp_path / 'ready'}\nexec sleep 120\n")
    git.chmod(0o755)
    env = {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "K3_STUB_VENV": "1",
        "K3_SKIP_TUI": "1",
        "K3_SKIP_GO": "1",
        "K3_NO_DOWNLOAD": "1",
        "K3_NO_GH": "1",
    }
    proc = subprocess.Popen(
        ["sh", str(INSTALL), "--from-source", "--minimal"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not (tmp_path / "ready").exists():
            assert proc.poll() is None, "install.sh ended before it reached git"
            assert time.monotonic() < deadline, "install.sh never reached git"
            time.sleep(0.05)
    except BaseException:  # the caller's try/finally starts only once this returns
        _kill_group(proc)
        raise
    return proc, tmp_path / DATA_REL / ".install.lock"


def _kill_group(proc: subprocess.Popen[bytes], sig: int = signal.SIGKILL) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, sig)
    proc.wait()


def test_k3code_update_stops_while_install_sh_holds_its_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc, lock = _hold_install_sh(tmp_path)
    try:
        monkeypatch.setenv("K3CODE_DATA", str(tmp_path / DATA_REL))
        assert (lock / "pid").read_text().strip() == str(proc.pid)
        if sys.platform == "linux":  # the shell and Python must read the same start time from /proc
            assert (lock / "start").read_text().strip() == upd._process_start(proc.pid)
        with pytest.raises(upd.InstallLockHeld, match=f"is running \\(pid {proc.pid}\\)"), upd.install_lock():
            pass
        assert (lock / "pid").read_text().strip() == str(proc.pid)  # the running install's lock is left alone
    finally:
        _kill_group(proc)


def test_k3code_update_takes_over_the_lock_of_a_killed_install_sh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc, lock = _hold_install_sh(tmp_path)
    _kill_group(proc)  # SIGKILL: the shell cannot clean up, the lock stays behind
    assert lock.is_dir()
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / DATA_REL))
    with upd.install_lock():
        assert (lock / "pid").read_text().strip() == str(os.getpid())
    assert not lock.exists()
