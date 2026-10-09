"""The suite never reaches the real systemd user manager: conftest puts logging stubs first on PATH."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_systemd_tools_on_path_are_the_conftest_stubs() -> None:
    for name in ("systemctl", "loginctl", "journalctl"):
        found = shutil.which(name)
        assert found is not None and b"Test stub" in Path(found).read_bytes()[:400], (name, found)
    # a script run with this PATH (install.sh, uninstall.sh) gets the stub too, and the call is logged
    subprocess.run(["sh", "-c", "systemctl --user daemon-reload"], env={"PATH": os.environ["PATH"]}, check=True)
    log = Path(shutil.which("systemctl") or "").parent / "calls.log"
    assert "systemctl --user daemon-reload" in log.read_text()


@pytest.mark.real_systemd
def test_a_test_can_opt_out_of_the_stubs(systemd_stub_dir: Path) -> None:
    assert str(systemd_stub_dir) not in os.environ["PATH"].split(os.pathsep)
