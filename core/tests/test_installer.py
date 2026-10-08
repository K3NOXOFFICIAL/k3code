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


def run(
    home: Path,
    script: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    drop: tuple[str, ...] = (),
    path_front: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),  # never read a real ~/.config/k3code/env
        "K3_STUB_VENV": "1",  # fake core venv: no pip / network
        "K3_SKIP_TUI": "1",
        "K3_SKIP_GO": "1",
        "K3_NO_DOWNLOAD": "1",
        "K3_NO_GH": "1",
    }
    for k in drop:
        env.pop(k, None)
    env.update(env_extra or {})
    if path_front is not None:
        env["PATH"] = f"{path_front}{os.pathsep}{env['PATH']}"
    # A new session has no controlling terminal, so the installer cannot open /dev/tty and prompt.
    return subprocess.run(
        ["sh", str(script), *args], env=env, capture_output=True, text=True, check=False, start_new_session=True
    )


def stub_bin(root: Path, name: str, body: str) -> Path:
    """A directory holding one executable stub (a shell script) named ``name``; put it first on PATH."""
    d = root / "stubbin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return d


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


def test_presetup_is_the_default_and_minimal_skips_it(tmp_path: Path) -> None:
    log = tmp_path / "curl.log"
    curl = stub_bin(tmp_path, "curl", f'echo "curl $*" >>"{log}"\nexit 1\n')
    default_home = tmp_path / "default"
    default_home.mkdir()
    r = run(default_home, INSTALL, "--from-source", "--yes", path_front=curl)
    assert r.returncode == 0, r.stderr
    assert "presetup (optional" in r.stderr
    assert not log.exists()  # under stubs presetup makes no network call

    minimal_home = tmp_path / "minimal"
    minimal_home.mkdir()
    m = run(minimal_home, INSTALL, "--from-source", "--yes", "--minimal", path_front=curl)
    assert m.returncode == 0, m.stderr
    assert "presetup (optional" not in m.stderr
    assert not log.exists()
    assert "Installed k3code" in m.stderr
