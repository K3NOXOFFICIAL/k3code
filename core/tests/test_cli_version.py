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


def test_installed_copy_reads_its_version_without_importlib_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """importlib.metadata is ~30 ms of an installed copy's import; the version comes from the dist-info instead."""
    import importlib.metadata
    import importlib.util

    import k3code._version as real

    site = tmp_path / "site"
    (site / "k3code").mkdir(parents=True)
    (site / "k3code" / "_version.py").write_text(Path(real.__file__).read_text())
    dist = site / "k3code-9.8.7.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.4\nName: k3code\nVersion: 9.8.7\n\nVersion: not-a-header\n")

    def boom(_name: str) -> str:
        raise AssertionError("importlib.metadata was used")

    monkeypatch.setattr(importlib.metadata, "version", boom)
    spec = importlib.util.spec_from_file_location("k3code_version_probe", site / "k3code" / "_version.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.__version__ == "9.8.7"
