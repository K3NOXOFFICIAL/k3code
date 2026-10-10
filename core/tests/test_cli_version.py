"""`k3code --version` names the running install's build (`0.1.0-src.<sha>`), and a dev checkout its plain version."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import __version__
from k3code.cli import cli


def test_version_names_the_recorded_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"{__version__}-src.abc1234"
    vdir = tmp_path / "versions" / name
    (vdir / "venv").mkdir(parents=True)
    (vdir / ".complete").write_text(name + "\n")  # what install.sh and `k3code update` write last
    monkeypatch.setattr(sys, "prefix", str(vdir / "venv"))
    r = CliRunner().invoke(cli, ["--version"])
    assert r.exit_code == 0, r.output
    assert r.output == f"k3code, version {name}\n"


def test_version_without_a_recorded_build_is_the_plain_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = tmp_path / "checkout" / "core" / ".venv"
    venv.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(venv))
    r = CliRunner().invoke(cli, ["--version"])
    assert r.exit_code == 0, r.output
    assert r.output == f"k3code, version {__version__}\n"
