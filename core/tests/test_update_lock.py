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


@pytest.mark.parametrize(
    ("recorded", "removed"),
    [
        ("tmp/k3code-tui.Ab12Cd", True),  # the control: a k3code-tui.* directly in TMPDIR goes
        ("tmp/not-k3code", False),
        ("tmp/sub/k3code-tui.Zz99", False),
        ("elsewhere/k3code-tui.Zz99", False),
    ],
)
def test_only_a_tui_build_dir_directly_in_tmpdir_is_removed_with_a_stale_lock(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str, recorded: str, removed: bool
) -> None:
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{_dead_pid()}\n")
    _tui_tmp(tmp_path, monkeypatch, lock, tmp_path / recorded)
    upd.install_release(_release(monkeypatch, []), None, uv=fake_uv)
    assert (tmp_path / recorded).exists() is not removed
    assert not lock.exists()


# What install.sh records in its lock (<kind>_tmp) for each scratch path it makes in TMPDIR as k3code-<kind>.XXXXXX.
SCRATCH_FILES = ("giterr", "reqs")
SCRATCH_KINDS = ("tui", "src", "giterr", "uv", "node", "go", "reqs")


def _scratch(tmp: Path, kind: str, name: str = "Ab12Cd") -> Path:
    path = tmp / f"k3code-{kind}.{name}"
    if kind in SCRATCH_FILES:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    else:
        (path / "sub").mkdir(parents=True)
    return path


@pytest.mark.parametrize("kind", SCRATCH_KINDS)
def test_a_stale_lock_takes_every_scratch_path_install_sh_recorded(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str, kind: str
) -> None:
    # a SIGKILLed install.sh cannot clean up: the checkout it fetched (with node_modules), its git error file, the
    # downloads for uv, Node and Go all stayed in TMPDIR for good
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{_dead_pid()}\n")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    path = _scratch(tmp_path / "tmp", kind)
    refs = path.with_name(path.name + ".refs")  # without git, the HTTPS ref list lives next to the git error file
    if kind == "giterr":
        refs.write_text("x\n")
    (lock / f"{kind}_tmp").write_text(f"{path}\n")
    upd.install_release(_release(monkeypatch, []), None, uv=fake_uv)
    assert not path.exists() and not refs.exists()
    assert not lock.exists()


@pytest.mark.parametrize(
    ("recorded", "removed"),
    [
        ("tmp/k3code-src.Ab12Cd", True),  # the control: a k3code-src.* directly in TMPDIR goes
        ("tmp/k3code-tui.Ab12Cd", False),  # another kind's name under this kind's record
        ("tmp/not-k3code", False),
        ("tmp/sub/k3code-src.Zz99", False),
        ("elsewhere/k3code-src.Zz99", False),
    ],
)
def test_only_a_scratch_path_of_the_recorded_kind_directly_in_tmpdir_is_removed(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str, recorded: str, removed: bool
) -> None:
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{_dead_pid()}\n")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / recorded).mkdir(parents=True)
    (lock / "src_tmp").write_text(f"{tmp_path / recorded}\n")
    upd.install_release(_release(monkeypatch, []), None, uv=fake_uv)
    assert (tmp_path / recorded).exists() is not removed
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


# -- `k3code update --rollback` honours the same lock -------------------------------------------------------------


def _versions(*names: str) -> None:
    """Version dirs with ``names[0]`` as ``previous`` and the last one as ``current``."""
    for n in names:
        (upd.versions_dir() / n).mkdir(parents=True)
    for n in names:
        upd.switch_to(n)


def _held_by_a_live_update(data: Path) -> Path:
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")  # a live process: an update or install.sh mid-build
    (lock / "start").write_text(f"{upd._process_start(os.getpid())}\n")
    return lock


def test_rollback_refuses_while_an_update_builds(data: Path) -> None:
    _versions("1.0.0", "1.1.0")
    lock = _held_by_a_live_update(data)
    restarted: list[str] = []
    res = upd.rollback(
        restart=lambda: restarted.append("r"), daemon_installed=lambda: True, daemon_stopped=lambda: False
    )
    assert not res.ok and not res.rolled_back
    assert "an update is running" in res.message
    assert (upd.current_version(), upd.previous_version()) == ("1.1.0", "1.0.0")  # nothing switched
    assert restarted == []  # the daemon was left alone
    assert (lock / "pid").read_text().strip() == str(os.getpid())  # and the running update keeps its lock


def test_rollback_during_a_build_is_not_overridden_by_the_update(
    data: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: str
) -> None:
    # the reported race: update A builds 9.0.0, `update --rollback` runs meanwhile, then A activates
    _versions("1.0.0", "1.1.0")
    rel = _release(monkeypatch, [])
    real_download = upd._download
    during: list[upd.UpdateResult] = []

    def download(url: str, dest: Path, tok: str | None) -> None:
        if not during:
            during.append(upd.rollback(daemon_installed=lambda: False))
        real_download(url, dest, tok)

    monkeypatch.setattr(upd, "_download", download)
    upd.install_release(rel, None, uv=fake_uv)
    assert not during[0].ok and "an update is running" in during[0].message
    assert upd.current_version() == "1.1.0"  # the refused rollback did not move `current` under the build
    upd.switch_to("9.0.0")  # what activate() does next
    assert (upd.current_version(), upd.previous_version()) == ("9.0.0", "1.1.0")


def test_rollback_holds_the_lock_while_it_switches_and_restarts(data: Path) -> None:
    _versions("1.0.0", "1.1.0")
    seen: list[tuple[str, str]] = []

    def restart() -> None:
        pid = data / LOCK / "pid"
        seen.append((pid.read_text().strip() if pid.is_file() else "", upd.current_version() or ""))

    res = upd.rollback(restart=restart, daemon_installed=lambda: True, daemon_stopped=lambda: False)
    assert res.ok and upd.current_version() == "1.0.0"
    assert seen == [(str(os.getpid()), "1.0.0")]  # an update cannot start building while the daemon restarts
    assert not (data / LOCK).exists()


def test_rollback_drops_the_lock_when_nothing_could_be_rolled_back(data: Path) -> None:
    _versions("1.0.0")
    res = upd.rollback(daemon_installed=lambda: False)
    assert not res.ok and "no previous version" in res.message
    assert not (data / LOCK).exists()


def test_a_lock_a_killed_update_left_does_not_block_a_rollback(data: Path) -> None:
    _versions("1.0.0", "1.1.0")
    lock = data / LOCK
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{_dead_pid()}\n")  # the process that took it is gone
    res = upd.rollback(daemon_installed=lambda: False)
    assert res.ok and upd.current_version() == "1.0.0"
    assert not lock.exists()
