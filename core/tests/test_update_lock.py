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


def _tui_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: Path, recorded: Path) -> Path:
    """A TUI build dir as install.sh makes it (TMPDIR/k3code-tui.XXXXXX) and the path its lock records."""
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    built = tmp_path / "tmp" / "k3code-tui.Ab12Cd"
    (built / "tui").mkdir(parents=True)
    recorded.mkdir(parents=True, exist_ok=True)
    (lock / "tui_tmp").write_text(f"{recorded}\n")
    return built


def test_a_lock_whose_pid_now_names_another_process_is_taken_over(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str
) -> None:
    # the killed install's pid went to an unrelated process (here this one): the start time tells them apart
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    (lock / "start").write_text("0\n")
    built = _tui_tmp(tmp_path, monkeypatch, lock, tmp_path / "tmp" / "k3code-tui.Ab12Cd")
    seen: list[str] = []
    vdir = upd.install_release(_release(monkeypatch, seen), None, uv=fake_uv)
    assert (vdir / ".complete").is_file() and set(seen) == {str(os.getpid())}
    assert not built.exists()  # the killed install's TUI build dir goes with its lock
    assert not lock.exists()


@pytest.mark.parametrize("recorded", ["tmp/not-k3code", "tmp/sub/k3code-tui.Zz99", "elsewhere/k3code-tui.Zz99"])
def test_a_recorded_path_that_is_not_a_tui_build_dir_is_left_alone(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str, recorded: str
) -> None:
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{_dead_pid()}\n")
    _tui_tmp(tmp_path, monkeypatch, lock, tmp_path / recorded)
    upd.install_release(_release(monkeypatch, []), None, uv=fake_uv)
    assert (tmp_path / recorded).is_dir()
    assert not lock.exists()


def test_a_live_pid_that_started_when_the_lock_says_still_refuses(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str
) -> None:
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    (lock / "start").write_text(f"{upd._process_start(os.getpid())}\n")
    built = _tui_tmp(tmp_path, monkeypatch, lock, tmp_path / "tmp" / "k3code-tui.Ab12Cd")
    with pytest.raises(upd.InstallLockHeld, match=f"is running \\(pid {os.getpid()}\\)"):
        upd.install_release(_release(monkeypatch, []), None, uv=fake_uv)
    assert built.is_dir() and (lock / "pid").is_file()  # a live install's lock and build dir are never touched


def test_the_lock_records_when_its_owner_started(data: Path) -> None:
    with upd.install_lock():
        start = (data / LOCK / "start").read_text().strip()
    assert start and start == upd._process_start(os.getpid())


def test_the_start_time_is_written_before_the_pid(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a kill between the two writes must not leave a pid without a start time (a recycled pid would keep the lock)
    present: list[bool] = []
    real = Path.write_text

    def spy(self: Path, text: str, *a: object, **kw: object) -> int:
        if self.name == "pid":
            present.append((self.parent / "start").is_file())
        return real(self, text, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy)
    with upd.install_lock():
        pass
    assert present == [True]


def test_a_lock_with_a_start_but_no_pid_still_refuses(data: Path) -> None:
    (data / LOCK).mkdir(parents=True)
    (data / LOCK / "start").write_text("123\n")
    with pytest.raises(upd.InstallLockHeld, match="without a pid"), upd.install_lock():
        pass


def test_the_start_time_is_counted_from_the_last_parenthesis() -> None:
    # the command name may hold spaces and ")": a naive split would read a different field
    rest = " ".join(["S", *[str(n) for n in range(4, 22)], "987654", "23", "24"])
    assert upd._stat_start(f"4242 (k3 (x) y)) {rest}\n".encode()) == "987654"
    assert upd._stat_start(b"4242 (cut short) S 1 2\n") == ""
