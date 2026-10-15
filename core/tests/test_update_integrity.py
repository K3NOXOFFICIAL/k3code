"""Update hardening: checksums fail closed, the smoke gate judges only the new version, restarts are checked."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import service
from k3code import update as upd
from k3code.cli import cli


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    return tmp_path


def _assets(tmp: Path, sums: str | None) -> dict[str, Path]:
    tmp.mkdir(parents=True, exist_ok=True)
    wheel = tmp / "k3code-9.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    files = {wheel.name: wheel}
    if sums is not None:
        (tmp / "SHA256SUMS").write_text(sums)
        files["SHA256SUMS"] = tmp / "SHA256SUMS"
    return files


def _sum(b: bytes, name: str) -> str:
    return f"{hashlib.sha256(b).hexdigest()}  {name}\n"


def test_checksums_fail_closed(tmp_path: Path) -> None:
    name = "k3code-9.0.0-py3-none-any.whl"
    with pytest.raises(upd.IntegrityError, match="no SHA256SUMS"):
        upd.verify_checksums(_assets(tmp_path / "a", None))
    with pytest.raises(upd.IntegrityError, match="no entry for"):
        upd.verify_checksums(_assets(tmp_path / "b", _sum(b"x", "other.tar.gz")))
    with pytest.raises(upd.IntegrityError, match="checksum mismatch"):
        upd.verify_checksums(_assets(tmp_path / "c", _sum(b"tampered", name)))
    upd.verify_checksums(_assets(tmp_path / "d", _sum(b"wheel", name)))  # a matching entry passes


@pytest.mark.parametrize("sums", [None, "", "0" * 64 + "  k3code-9.0.0-py3-none-any.whl\n"])
def test_cli_refuses_unverifiable_release_cleanly(
    data: Path, monkeypatch: pytest.MonkeyPatch, sums: str | None
) -> None:
    assets = {"k3code-9.0.0-py3-none-any.whl": "u/whl", "k3code-9.0.0-requirements.txt": "u/reqs"}
    if sums is not None:
        assets["SHA256SUMS"] = "u/sums"
    rel = upd.Release(tag="v9.0.0", version="9.0.0", body="", prerelease=False, assets=assets)
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: rel)
    monkeypatch.setattr(
        upd, "_download", lambda url, dest, tok: dest.write_bytes(b"wheel" if "whl" in url else (sums or "").encode())
    )
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit), r.output
    assert "update refused" in r.output and "Traceback" not in r.output
    assert not (data / "versions" / "9.0.0").exists() and not (data / "current").exists()


def _version(data: Path, ver: str, checks: list[dict[str, str]], said: str | None = None) -> Path:
    d = data / "versions" / ver
    exe = d / "venv" / "bin" / "k3code"
    exe.parent.mkdir(parents=True)
    report = json.dumps({"summary": {"fail": sum(c["status"] == "fail" for c in checks)}, "checks": checks})
    exe.write_text(
        f"#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'k3code, version {said or ver}'; exit 0; fi\n"
        f"echo '{report}'; exit 1\n"
    )
    exe.chmod(0o755)
    return d


def test_environment_failures_do_not_block_activation(data: Path) -> None:
    env_fails = [
        {"name": "providers", "status": "fail"},
        {"name": "tui", "status": "fail"},
        {"name": "api-keys", "status": "fail"},
        {"name": "vendor", "status": "fail"},
        {"name": "disk", "status": "fail"},
    ]
    _version(data, "1.0.0", [])
    upd.switch_to("1.0.0")
    _version(data, "1.1.0", env_fails)
    res = upd.activate("1.1.0", daemon_installed=lambda: False)
    assert res.ok and upd.current_version() == "1.1.0", res.message


def test_failed_smoke_test_removes_the_staged_version(data: Path) -> None:
    _version(data, "1.0.0", [])
    upd.switch_to("1.0.0")
    staged = _version(data, "1.1.0", [{"name": "k3code-home", "status": "fail"}])
    res = upd.activate("1.1.0", daemon_installed=lambda: False)
    assert not res.ok and "k3code-home" in res.message
    assert not staged.exists() and upd.current_version() == "1.0.0"
    wrong = _version(data, "1.2.0", [], said="1.0.0")  # the venv runs some other version's code
    assert not upd.activate("1.2.0", daemon_installed=lambda: False).ok and not wrong.exists()


def test_restart_resets_the_start_limit_and_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    rc = {"restart": 0}

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, rc.get(args[0], 0), "", "start-limit-hit")

    monkeypatch.setattr(service, "_systemctl", fake)
    service.restart()
    assert calls == [("reset-failed", service.UNIT_NAME), ("restart", service.UNIT_NAME)]
    rc["restart"] = 1
    with pytest.raises(service.ServiceError, match="start-limit-hit"):
        service.restart()


def test_rollback_reports_a_daemon_that_does_not_come_back(data: Path) -> None:
    _version(data, "1.0.0", [])
    _version(data, "1.1.0", [])
    upd.switch_to("1.0.0")

    def refuse() -> None:
        raise service.ServiceError("systemctl --user restart k3code.service failed: start-limit-hit")

    res = upd.activate(
        "1.1.0",
        restart=refuse,
        daemon_installed=lambda: True,
        daemon_stopped=lambda: False,
        manager_reachable=lambda: True,  # systemd answered and refused: a real failure
        wait=lambda h: True,
    )
    assert not res.ok and res.rolled_back and "did not restart" in res.message
    assert upd.current_version() == "1.0.0"


def _no_bus() -> None:
    raise service.ServiceError("systemctl --user restart k3code.service failed: Failed to connect to bus")


def test_an_unreachable_user_manager_keeps_the_new_version_and_warns(data: Path) -> None:
    """#115: a unit file on a box whose user manager does not answer (WSL without systemd, `su`) is no daemon to
    restart; the smoke-tested version stays instead of every update rolling back."""
    _version(data, "1.0.0", [])
    _version(data, "1.1.0", [])
    upd.switch_to("1.0.0")
    res = upd.activate(
        "1.1.0",
        restart=_no_bus,
        daemon_installed=lambda: True,
        daemon_stopped=lambda: False,
        manager_reachable=lambda: False,
        wait=lambda h: True,
    )
    assert res.ok and not res.rolled_back and upd.current_version() == "1.1.0"
    assert upd.MANAGER_UNREACHABLE in res.message


def test_rollback_with_an_unreachable_user_manager_keeps_the_switch_and_warns(data: Path) -> None:
    _version(data, "1.0.0", [])
    _version(data, "1.1.0", [])
    upd.switch_to("1.0.0")
    upd.switch_to("1.1.0")
    res = upd.rollback(
        restart=_no_bus, daemon_installed=lambda: True, daemon_stopped=lambda: False, manager_reachable=lambda: False
    )
    assert res.ok and res.rolled_back and upd.current_version() == "1.0.0"
    assert upd.MANAGER_UNREACHABLE in res.message


def test_service_install_with_an_unreachable_user_manager_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    calls: list[tuple[str, ...]] = []

    def no_bus(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "Failed to connect to bus: No medium found")

    monkeypatch.setattr(service, "systemd_available", lambda: True)
    monkeypatch.setattr(service, "_systemctl", no_bus)
    assert not service.manager_reachable()
    r = CliRunner().invoke(cli, ["service", "install"])
    assert r.exit_code == 1 and "not reachable" in r.output and "Traceback" not in r.output
    assert not (tmp_path / "cfg").exists() and calls == [("show-environment",), ("show-environment",)]


def test_the_wizard_service_step_reports_an_unreachable_manager(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from k3code.setup import steps

    def refuse() -> list[str]:
        raise service.ServiceError("systemd user manager not reachable")

    said: list[str] = []
    monkeypatch.setattr(service, "install", refuse)
    ctx = SimpleNamespace(p=SimpleNamespace(confirm=lambda *a: True), say=said.append)
    assert steps.step_service(ctx) == {"install": False}
    assert any("not reachable" in line for line in said)


def test_service_install_without_systemd_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    r = CliRunner().invoke(cli, ["service", "install"])
    assert r.exit_code == 1 and "systemd user manager not available" in r.output
    assert "Traceback" not in r.output and not (tmp_path / "cfg").exists()


def test_fetch_latest_honours_the_api_override(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class R:
        status_code = 200

        def raise_for_status(self) -> None: ...

        def json(self) -> list[object]:
            return []

    monkeypatch.setattr(upd.httpx, "get", lambda url, **k: seen.append(url) or R())
    monkeypatch.setenv("K3CODE_UPDATE_API", "http://127.0.0.1:9/mirror/")
    upd.fetch_latest(repo="o/r")
    assert seen == ["http://127.0.0.1:9/mirror/repos/o/r/releases?per_page=30"]


@pytest.mark.parametrize(
    ("status", "headers"),
    [(403, {"x-ratelimit-remaining": "0", "retry-after": "60"}), (429, {"retry-after": "60"})],
)
def test_fetch_latest_reports_a_rate_limit_not_a_private_repo(monkeypatch, status, headers) -> None:
    """#143: GitHub answers an exhausted unauthenticated limit with 403 (or 429); that is not 'private or missing'."""
    resp = upd.httpx.Response(status, headers=headers, json={"message": "API rate limit exceeded"})
    monkeypatch.setattr(upd.httpx, "get", lambda url, **k: resp)
    with pytest.raises(RuntimeError, match="rate limit") as e:
        upd.fetch_latest(repo="o/r")
    assert "private" not in str(e.value) and "retry after 60s" in str(e.value)


def test_fetch_latest_still_calls_a_plain_403_private(monkeypatch) -> None:
    resp = upd.httpx.Response(403, headers={"x-ratelimit-remaining": "42"}, json={})
    monkeypatch.setattr(upd.httpx, "get", lambda url, **k: resp)
    with pytest.raises(PermissionError, match="private or does not exist"):
        upd.fetch_latest(repo="o/r")
