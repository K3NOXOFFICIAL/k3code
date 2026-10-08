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
        if ".k3code" in p.parts or p.name == "install.log":
            continue  # doctor may touch its own home dir; the install log grows on every run by design
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


def test_from_git_fetch_failure_leaves_no_install(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-repo.git"  # a local path that is not a repository: fetch fails offline
    r = run(tmp_path, INSTALL, "--yes", "--from-git", f"file://{missing}", "--ref", "Main")
    assert r.returncode != 0
    assert "could not fetch 'Main'" in r.stderr
    assert not (tmp_path / ".local" / "share" / "k3code" / "versions").exists()
    assert not (tmp_path / ".local" / "bin" / "k3code").exists()


def test_unknown_option_is_rejected(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--bogus")
    assert r.returncode != 0 and "unknown option" in r.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_installed_requirements_are_the_locked_runtime_set() -> None:
    # install_core_copy installs `uv export --locked --no-dev`; the stubbed installer tests never run that path.
    uv = shutil.which("uv") or "uv"
    core = str(REPO / "core")
    lock = subprocess.run([uv, "lock", "--check", "--offline", "--project", core], capture_output=True)
    assert lock.returncode == 0, lock.stderr
    export = subprocess.run(
        [uv, "export", "--project", core, "--locked", "--no-dev", "--no-hashes", "--no-emit-project"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in export.stdout.splitlines() if "==" in ln and not ln.startswith(" ")]
    names = {ln.split("==")[0].strip().lower() for ln in lines}
    assert {"mcp", "pydantic", "click", "pyyaml", "prompt-toolkit"} <= names
    assert not names & {"pytest", "pytest-asyncio", "ruff", "respx", "pexpect"}


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


def test_windows_shell_points_to_install_ps1(tmp_path: Path) -> None:
    # Git Bash / MSYS / Cygwin: install.sh cannot install there and says to use install.ps1 (WSL) instead.
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uname").write_text('#!/bin/sh\n[ "$1" = -s ] && echo MINGW64_NT-10.0-19045 || echo x86_64\n')
    (fake / "uname").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    r = subprocess.run(["sh", str(INSTALL), "--check"], env=env, capture_output=True, text=True, check=False)
    assert r.returncode != 0
    assert "install.ps1" in r.stderr


FAKE_WSL = """#!/bin/sh
# wsl.exe stand-in: lists one distro, and runs --exec commands here (honouring --cd)
if [ "$1" = --list ]; then printf '  NAME      STATE           VERSION\\n* Ubuntu    Running         2\\n'; exit 0; fi
while [ $# -gt 0 ]; do
  case "$1" in
    -d) shift ;;
    --cd) cd "$2" || exit 9; shift ;;
    --exec) shift; exec "$@" ;;
  esac
  shift
done
"""


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell (pwsh)")
def test_install_ps1_runs_install_sh_in_wsl_and_writes_shims(tmp_path: Path) -> None:
    wsl = tmp_path / "wsl"
    wsl.write_text(FAKE_WSL)
    wsl.chmod(0o755)
    appdata = tmp_path / "appdata"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LOCALAPPDATA": str(appdata),
        "K3_WSL": str(wsl),
        "K3_STUB_VENV": "1",
        "K3_SKIP_TUI": "1",
        "K3_SKIP_GO": "1",
        "K3_NO_DOWNLOAD": "1",
    }

    def ps(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["pwsh", "-NoProfile", "-File", str(REPO / "install" / script), *args]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)

    r = ps("install.ps1", "-NoModifyPath", "--from-source", "--yes")
    assert r.returncode == 0, r.stderr
    assert "WSL distribution: Ubuntu" in r.stderr
    assert (tmp_path / ".local" / "share" / "k3code" / "current").is_symlink()
    shim = (appdata / "k3code" / "bin" / "k3code.cmd").read_text()
    assert f'--exec sh -lc "exec {tmp_path}/.local/bin/k3code \\"$@\\"" k3code %*' in shim
    assert not (appdata / "k3code" / "bin" / "k3.cmd").exists()  # no k3 binary was built

    r = ps("uninstall.ps1")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".local" / "share" / "k3code").exists()
    assert not (appdata / "k3code").exists()
