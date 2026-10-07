from __future__ import annotations

from pathlib import Path

import pytest

from k3code import update as upd


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    return tmp_path


def make_version(data: Path, ver: str, *, version_ok: bool = True, fails: int = 0) -> Path:
    d = data / "versions" / ver
    exe = d / "venv" / "bin" / "k3code"
    exe.parent.mkdir(parents=True)
    exe.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "k3code, version {ver}"; exit {0 if version_ok else 3}; fi\n'
        f'if [ "$1" = "doctor" ]; then echo \'{{"summary": {{"ok": 5, "warn": 0, "fail": {fails}}}, "checks": []}}\'; '
        f"exit {1 if fails else 0}; fi\n"
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
