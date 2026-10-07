from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "install" / "install.sh"
UNINSTALL = REPO / "install" / "uninstall.sh"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="installer needs uv present")


def run(home: Path, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "K3_STUB_VENV": "1",  # fake core venv: no pip / network
        "K3_SKIP_TUI": "1",
        "K3_SKIP_GO": "1",
        "K3_NO_DOWNLOAD": "1",
        "K3_NO_GH": "1",
    }
    return subprocess.run(["sh", str(script), *args], env=env, capture_output=True, text=True, check=False)


def snapshot(home: Path) -> dict[str, tuple[float, str]]:
    out = {}
    for p in sorted(home.rglob("*")):
        if ".k3code" in p.parts:
            continue  # doctor may touch its own home dir
        out[str(p.relative_to(home))] = (p.lstat().st_mtime_ns, os.readlink(p) if p.is_symlink() else "")
    return out


def test_install_layout_and_idempotent(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--from-source", "--yes", "--no-setup")
    assert r.returncode == 0, r.stderr
    data = tmp_path / ".local" / "share" / "k3code"
    (ver,) = [p.name for p in (data / "versions").iterdir()]
    assert ver.startswith((REPO / "VERSION").read_text().strip())
    assert (data / "current").is_symlink() and (data / "current").resolve().name == ver
    assert (tmp_path / ".local" / "bin" / "k3code").is_symlink()
    assert (data / "versions" / ver / ".complete").is_file()
    assert (data / "source_path").read_text().strip() == str(REPO)
    before = snapshot(tmp_path)
    r2 = run(tmp_path, INSTALL, "--from-source", "--yes", "--no-setup")
    assert r2.returncode == 0, r2.stderr
    assert "already installed" in r2.stderr
    assert snapshot(tmp_path) == before  # second run changes nothing


def test_install_needs_yes_or_token(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL)  # release mode without a token
    assert r.returncode != 0 and "GITHUB_TOKEN" in r.stderr
    r = run(tmp_path, INSTALL, "--bogus")
    assert r.returncode != 0 and "unknown option" in r.stderr


def test_uninstall_keeps_user_data_unless_purge(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--yes", "--no-setup").returncode == 0
    (tmp_path / ".k3code").mkdir(exist_ok=True)
    (tmp_path / ".k3code" / "config.yaml").write_text("x: 1\n")
    r = run(tmp_path, UNINSTALL)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".local" / "share" / "k3code").exists()
    assert not (tmp_path / ".local" / "bin" / "k3code").exists()
    assert (tmp_path / ".k3code" / "config.yaml").is_file()
    assert run(tmp_path, UNINSTALL, "--purge").returncode == 0
    assert not (tmp_path / ".k3code").exists()
