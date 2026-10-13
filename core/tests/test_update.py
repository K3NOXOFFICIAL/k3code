from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from k3code import update as upd


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    return tmp_path


#: a ``doctor`` stub body that reports nothing wrong
DOCTOR_OK = 'echo \'{"summary": {"ok": 1, "warn": 0, "fail": 0}, "checks": []}\''


def make_version(data: Path, ver: str, *, version_ok: bool = True, fails: int = 0, doctor: str = "") -> Path:
    """A version directory whose ``k3code`` is a shell stub; ``doctor`` replaces what its ``doctor`` command runs."""
    d = data / "versions" / ver
    exe = d / "venv" / "bin" / "k3code"
    exe.parent.mkdir(parents=True)
    doctor = doctor or (
        f'echo \'{{"summary": {{"ok": 5, "warn": 0, "fail": {fails}}}, "checks": []}}\'; exit {1 if fails else 0}'
    )
    exe.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "k3code, version {ver}"; exit {0 if version_ok else 3}; fi\n'
        f'if [ "$1" = "doctor" ]; then {doctor}; fi\n'
    )
    exe.chmod(0o755)
    return d


def test_switch_records_previous(data: Path) -> None:
    make_version(data, "1.0.0")
    make_version(data, "1.1.0")
    upd.switch_to("1.0.0")
    assert upd.current_version() == "1.0.0" and upd.previous_version() is None
    upd.switch_to("1.1.0")
    assert upd.current_version() == "1.1.0" and upd.previous_version() == "1.0.0"
    assert (data / "current").resolve() == (data / "versions" / "1.1.0").resolve()


def test_smoke_pass_and_fail(data: Path) -> None:
    assert upd.smoke_test(make_version(data, "1.0.0"))[0]
    assert not upd.smoke_test(make_version(data, "2.0.0", fails=1))[0]
    assert not upd.smoke_test(make_version(data, "3.0.0", version_ok=False))[0]
    assert not upd.smoke_test(data / "versions" / "nope")[0]


def test_smoke_does_not_run_in_the_callers_cwd(data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`k3code update` started at a Windows drive root refused a healthy version: doctor scans its cwd and died on
    /mnt/c/pagefile.sys (#157). The gate checks the new version, so it runs inside that version, not wherever the
    update was started."""
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "pagefile.sys").write_text("")
    version = make_version(
        data,
        "1.0.0",
        doctor=('if [ -e pagefile.sys ]; then echo "PermissionError: pagefile.sys" >&2; exit 1; fi; ' + DOCTOR_OK),
    )
    monkeypatch.chdir(drive)
    ok, why = upd.smoke_test(version)
    assert ok, why


def test_smoke_with_a_relative_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The children run inside the version: a relative K3CODE_DATA must not be read again from there."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("K3CODE_DATA", "rel-data")
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    version = make_version(
        tmp_path / "rel-data",
        "1.0.0",
        doctor=('[ -d "$K3CODE_DATA/versions" ] || exit 7; ' + DOCTOR_OK),
    )
    ok, why = upd.smoke_test(version.relative_to(tmp_path))
    assert ok, why


def test_smoke_failure_keeps_the_end_of_stderr(data: Path) -> None:
    """A traceback names its cause on the last line; the first 200 characters of it were only the frames (#157)."""
    version = make_version(data, "1.0.0", doctor='cat "$(dirname "$0")/doctor.err" >&2; exit 1')
    frames = "Traceback (most recent call last):\n" + '  File "x.py", line 1, in f\n    g()\n' * 30
    (version / "venv" / "bin" / "doctor.err").write_text(frames + "PermissionError: [Errno 13] Permission denied\n")
    ok, why = upd.smoke_test(version)
    assert not ok
    assert "PermissionError: [Errno 13] Permission denied" in why
    assert len(why) < 600


def test_activate_success_restarts_daemon(data: Path) -> None:
    make_version(data, "1.0.0")
    make_version(data, "1.1.0")
    upd.switch_to("1.0.0")
    calls: list[str] = []
    res = upd.activate(
        "1.1.0", restart=lambda: calls.append("restart"), daemon_installed=lambda: True, wait=lambda h: True
    )
    assert res.ok and upd.current_version() == "1.1.0" and calls == ["restart"]


def test_smoke_failure_does_not_switch(data: Path) -> None:
    make_version(data, "1.0.0")
    make_version(data, "1.1.0", fails=2)
    upd.switch_to("1.0.0")
    res = upd.activate("1.1.0", daemon_installed=lambda: False)
    assert not res.ok and "smoke test failed" in res.message
    assert upd.current_version() == "1.0.0"


def test_daemon_failure_rolls_back(data: Path) -> None:
    make_version(data, "1.0.0")
    make_version(data, "1.1.0")
    upd.switch_to("1.0.0")
    calls: list[str] = []
    res = upd.activate("1.1.0", restart=lambda: calls.append("r"), daemon_installed=lambda: True, wait=lambda h: False)
    assert not res.ok and res.rolled_back
    assert upd.current_version() == "1.0.0"
    assert calls == ["r", "r"]  # restarted into the new version, then again after rollback


def test_manual_rollback_toggles(data: Path) -> None:
    make_version(data, "1.0.0")
    make_version(data, "1.1.0")
    upd.switch_to("1.0.0")
    upd.switch_to("1.1.0")
    res = upd.rollback(daemon_installed=lambda: False)
    assert res.ok and upd.current_version() == "1.0.0"
    assert upd.rollback(daemon_installed=lambda: False).ok and upd.current_version() == "1.1.0"


def test_rollback_without_previous(data: Path) -> None:
    make_version(data, "1.0.0")
    upd.switch_to("1.0.0")
    assert not upd.rollback(daemon_installed=lambda: False).ok


def test_wait_healthy_times_out_and_succeeds() -> None:
    t = {"now": 0.0}

    def sleep(s: float) -> None:
        t["now"] += s

    assert not upd.wait_healthy(lambda: False, timeout=120, sleep=sleep, clock=lambda: t["now"])
    assert t["now"] >= 120
    assert upd.wait_healthy(lambda: True, timeout=120, sleep=sleep, clock=lambda: t["now"])


def test_prune_keeps_current_and_previous(data: Path) -> None:
    for v in ("0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0"):
        make_version(data, v)
    upd.switch_to("0.1.0")
    upd.switch_to("0.5.0")
    removed = upd.prune(keep=2)
    assert set(upd.installed_versions()) == {"0.1.0", "0.5.0", "0.4.0"}
    assert "0.1.0" not in removed


def test_version_ordering() -> None:
    k = upd.version_key
    assert k("0.10.0") > k("0.9.0") > k("0.9.0-dev.1")
    assert k("v1.0.0") == k("1.0.0")


def test_prerelease_identifiers_compare_numerically() -> None:
    assert upd.is_newer("1.0.0-dev.10", "1.0.0-dev.9")  # compared as strings, "10" < "9"
    k = upd.version_key
    assert k("1.0.0-alpha") < k("1.0.0-alpha.1") < k("1.0.0-alpha.beta") < k("1.0.0-beta") < k("1.0.0-beta.2")
    assert k("1.0.0-beta.2") < k("1.0.0-beta.11") < k("1.0.0-rc.1") < k("1.0.0")


# ── regressions from the long-run audit ──


def _release(ver: str) -> upd.Release:
    return upd.Release(tag=f"v{ver}", version=ver, body="", prerelease=False, assets={})


def test_install_release_never_deletes_the_live_install(data: Path) -> None:
    """`/update now` called install_release() with no version check; it rmtree'd versions/<ver> even when that was the
    current version, deleting the running k3code before the (failing) rebuild."""
    live = make_version(data, "1.0.0")
    (live / ".complete").write_text("1.0.0\n")
    upd.switch_to("1.0.0")
    marker = live / "venv" / "keep.me"
    marker.write_text("x")
    rel = _release("1.0.0")
    assert upd.install_release(rel, None) == live  # complete: returned as is, nothing downloaded or rebuilt
    assert marker.exists()
    (live / ".complete").unlink()  # even incomplete, the active version is protected
    with pytest.raises(ValueError, match="active or the previous"):
        upd.install_release(rel, None)
    assert marker.exists()


def test_is_newer_compares_against_the_installed_version(data: Path) -> None:
    assert upd.is_newer("1.2.0", "1.1.0") and not upd.is_newer("1.1.0", "1.1.0") and not upd.is_newer("1.0.9", "1.1.0")
    assert not upd.is_newer("0.0.1", "0.0.1-src.abc1234")  # a source install of 0.0.1 is that release
    assert upd.is_newer("0.0.2", "0.0.1-src.abc1234") and upd.is_newer("0.0.1", None)


async def test_update_now_when_up_to_date_changes_nothing(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code.commands.update_cmd import UpdateCommand

    make_version(data, "1.0.0")
    upd.switch_to("1.0.0")
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: _release("1.0.0"))
    monkeypatch.setattr(upd, "apply_detached", lambda: pytest.fail("must not update when already current"))
    out = await UpdateCommand().handle(None, None, "now")
    assert "Already up to date" in out["output"]


def test_apply_detached_runs_outside_the_service_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(upd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(upd.subprocess, "run", lambda argv, **k: seen.append(list(argv)) or R())
    msg = upd.apply_detached(unit_installed=lambda: True)
    assert seen and seen[0][:3] == ["/usr/bin/systemd-run", "--user", "--collect"]
    assert seen[0][-2:] == ["update", "--yes"] and "background" in msg
    monkeypatch.setattr(upd.shutil, "which", lambda name: None if name == "systemd-run" else "/x/k3code")
    assert "systemd-run is not available" in upd.apply_detached(unit_installed=lambda: True)


def test_apply_detached_without_a_unit_needs_no_systemd(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS and WSL without systemd have no systemd-run: `/update now` used to answer "run it from a terminal".
    With no service unit there is nothing to restart, so the update runs as a detached process, logged to a file."""
    import time

    out = data / "ran.txt"
    exe = data / "k3code"
    sid = data / "sid.txt"  # its own session: closing the terminal (SIGHUP to the session) does not stop it
    exe.write_text(
        f'#!/bin/sh\n{shlex.quote(sys.executable)} -c "import os; print(os.getsid(0))" > {sid}\n'
        f'echo "$@ sock=${{K3CODE_GATEWAY_SOCKET:-none}}" > {out}\necho updating\n'
    )
    exe.chmod(0o755)
    monkeypatch.setenv("K3CODE_GATEWAY_SOCKET", "/run/secret.sock")
    monkeypatch.setattr(upd.shutil, "which", lambda name: str(exe) if name == "k3code" else None)
    msg = upd.apply_detached(unit_installed=lambda: False)
    assert "background" in msg and str(upd.update_log_path()) in msg
    deadline = time.monotonic() + 10
    while not (out.exists() and "updating" in upd.update_log_path().read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert out.read_text().strip() == "update --yes sock=none"  # the daemon's socket is never handed to the child
    assert "updating" in upd.update_log_path().read_text()
    assert int(sid.read_text()) != os.getsid(0)
