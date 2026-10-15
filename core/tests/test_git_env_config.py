"""Tests run git with a consistent environment config (the host's GIT_CONFIG_* set is dropped as a whole)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_no_partial_git_env_config_reaches_a_test() -> None:
    assert not [n for n in os.environ if n.startswith("GIT_CONFIG_")]


def test_git_init_works_in_a_test(tmp_path: Path) -> None:
    r = subprocess.run(["git", "init", "-q", str(tmp_path / "r")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
