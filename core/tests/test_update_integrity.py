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

    res = upd.activate("1.1.0", restart=refuse, daemon_installed=lambda: True, wait=lambda h: True)
    assert not res.ok and res.rolled_back and "did not restart" in res.message
    assert upd.current_version() == "1.0.0"


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
