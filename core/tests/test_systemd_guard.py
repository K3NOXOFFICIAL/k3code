"""The suite never reaches the real systemd user manager: conftest puts logging stubs first on PATH."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import SYSTEMD_TOOLS


def test_systemd_tools_on_path_are_the_conftest_stubs(systemd_stub_dir: Path) -> None:
    for name in SYSTEMD_TOOLS:
        assert shutil.which(name) == str(systemd_stub_dir / name), name
    # a script run with this PATH (install.sh, uninstall.sh) gets the stub too, and the call is logged
    env = {"PATH": os.environ["PATH"]}
    subprocess.run(["sh", "-c", "systemctl --user daemon-reload"], env=env, check=True)
    assert "systemctl --user daemon-reload" in (systemd_stub_dir / "calls.log").read_text()


@pytest.mark.real_systemd
def test_a_test_can_opt_out_of_the_stubs(systemd_stub_dir: Path) -> None:
    assert str(systemd_stub_dir) not in os.environ["PATH"].split(os.pathsep)
