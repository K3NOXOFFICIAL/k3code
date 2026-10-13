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


def test_a_real_systemd_test_is_in_the_serial_lane() -> None:
    # scripts/ci/check.sh runs `-m serial` alone after the parallel xdist lane: a test on the host's systemd
    # programs must be in it, whether or not it carries the marker by hand
    import conftest

    class Item:
        def __init__(self, *names: str) -> None:
            self.names, self.added = set(names), []

        def get_closest_marker(self, name: str):
            return object() if name in self.names else None

        def add_marker(self, marker) -> None:
            self.added.append(marker.name)

    real, plain = Item("real_systemd"), Item()
    conftest.pytest_collection_modifyitems([real, plain])
    assert real.added == ["serial"] and plain.added == []
