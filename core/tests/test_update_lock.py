"""`k3code update` builds versions/<ver> under install.sh's .install.lock, so the two never build at once."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from k3code import update as upd

WHEEL = "k3code-9.0.0-py3-none-any.whl"
REQS = "k3code-9.0.0-requirements.txt"
LOCK = ".install.lock"  # install/install.sh's take_lock


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    return tmp_path / "data"


@pytest.fixture
def fake_uv(tmp_path: Path) -> str:
    exe = tmp_path / "bin" / "uv"
    exe.parent.mkdir()
    exe.write_text('#!/bin/sh\nif [ "$1" = venv ]; then mkdir -p "$4/bin"; fi\n')
    exe.chmod(0o755)
    return str(exe)


def _release(monkeypatch: pytest.MonkeyPatch, lock_pids: list[str]) -> upd.Release:
    """A pinned 9.0.0 release; each download records the pid in the install lock at that moment."""
    content = {WHEEL: b"wheel", REQS: b"httpx==0.28.1 --hash=sha256:" + b"0" * 64 + b"\n"}
    sums = "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in content.items())
    by_url = {**{f"u/{n}": b for n, b in content.items()}, "u/SHA256SUMS": sums.encode()}

    def download(url: str, dest: Path, tok: str | None) -> None:
        pid = upd.data_dir() / LOCK / "pid"
        lock_pids.append(pid.read_text().strip() if pid.is_file() else "")
        dest.write_bytes(by_url[url])

    monkeypatch.setattr(upd, "_download", download)
    assets = {n: f"u/{n}" for n in [*content, "SHA256SUMS"]}
    return upd.Release(tag="v9.0.0", version="9.0.0", body="", prerelease=False, assets=assets)


def _dead_pid() -> int:
    return int(subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True, check=True).stdout)


def test_the_build_holds_the_install_lock_and_drops_it(
    data: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str
) -> None:
    seen: list[str] = []
    vdir = upd.install_release(_release(monkeypatch, seen), None, uv=fake_uv)
    assert (vdir / ".complete").is_file()
    assert seen and set(seen) == {str(os.getpid())}  # install.sh sees a live pid for the whole build
    assert not (data / LOCK).exists()


def test_a_running_install_stops_the_update_before_it_builds(
    data: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str
) -> None:
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")  # a live process: install.sh mid-install
    half = data / "versions" / "9.0.0" / "venv"
    half.mkdir(parents=True)  # what that install is building right now
    seen: list[str] = []
    with pytest.raises(OSError, match=f"another install into .* is running \\(pid {os.getpid()}\\)"):
        upd.install_release(_release(monkeypatch, seen), None, uv=fake_uv)
    assert seen == [] and half.is_dir()  # nothing downloaded, the installer's half-built version left alone
    assert (lock / "pid").read_text() == f"{os.getpid()}\n"  # someone else's lock is never removed


def test_a_lock_without_a_pid_stops_the_update(data: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str) -> None:
    (data / LOCK).mkdir(parents=True)  # install.sh between mkdir and writing its pid
    with pytest.raises(OSError, match="without a pid .*rm -r"):
        upd.install_release(_release(monkeypatch, []), None, uv=fake_uv)
    assert (data / LOCK).is_dir()


def test_the_lock_of_a_killed_install_is_taken_over(data: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str) -> None:
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{_dead_pid()}\n")
    seen: list[str] = []
    vdir = upd.install_release(_release(monkeypatch, seen), None, uv=fake_uv)
    assert (vdir / ".complete").is_file() and set(seen) == {str(os.getpid())}
    assert not lock.exists()
