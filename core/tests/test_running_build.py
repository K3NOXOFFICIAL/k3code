"""A process keeps running the build it started as when `current` moves on under it. It must know that build, report it
(daemon version, TUI banner, doctor), and `/update` and `k3code update` must say "restart to use X" instead of
"Already up to date"."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import m1cmd_helpers as m1
from k3code import _version, doctor
from k3code import update as upd
from k3code.cli import cli
from k3code.commands.update_cmd import UpdateCommand

OLD = "0.1.0-src.0db792f"
NEW = "0.1.0-src.92aefd1"
HEAD = "92aefd1" + "0" * 33  # the full sha the git remote reports for the branch


def _install(data: Path, *names: str) -> None:
    """Version dirs as install.sh leaves them: the venv inside, the name in `.complete` written last."""
    for name in names:
        vdir = data / "versions" / name
        (vdir / "venv").mkdir(parents=True)
        (vdir / ".complete").write_text(name + "\n")


def _point_current_at(data: Path, name: str) -> None:
    link = data / "current"
    link.unlink(missing_ok=True)
    link.symlink_to(data / "versions" / name)


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("K3CODE_DATA", str(d))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: None)  # no release: the git ref is the source
    return d


@pytest.fixture
def started_on_old_then_updated(data: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The daemon started through `current` on OLD; a shell update then moved `current` to NEW under it."""
    _install(data, OLD, NEW)
    (data / "versions" / NEW / ".ref").write_text("main\n")
    _point_current_at(data, OLD)
    monkeypatch.setattr(sys, "prefix", str(data / "current" / "venv"))
    importlib.reload(_version)  # process start: k3code is imported while `current` still names OLD
    _point_current_at(data, NEW)
    return data


def test_a_running_process_keeps_the_build_it_started_as(started_on_old_then_updated: Path) -> None:
    assert upd.current_version() == NEW
    assert _version.build_version() == OLD


async def test_the_daemon_reports_its_build_not_the_plain_version(
    started_on_old_then_updated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    try:
        assert (await m1.rpc(srv, "setup.status"))["result"]["version"] == OLD  # doctor compares it with `current`
        info = (await m1.rpc(srv, "session.create", {"cwd": str(tmp_path)}))["result"]["info"]
        assert info["version"] == OLD  # the TUI banner shows it
    finally:
        await srv.close()


def test_doctor_tells_two_builds_of_one_version_apart(started_on_old_then_updated: Path) -> None:
    chk = doctor.check_daemon_version(_version.build_version())
    assert chk.status == doctor.WARN and OLD in chk.detail and NEW in chk.detail and "restart" in chk.fix
    assert doctor.check_daemon_version(NEW).status == doctor.OK


async def test_slash_update_says_to_restart_when_the_running_build_is_not_current(
    started_on_old_then_updated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported case: the TUI popup said 'current: ...92aefd1 / latest: 92aefd1 / Already up to date.' while the
    daemon still executed 0db792f."""
    monkeypatch.setattr(upd, "remote_head", lambda url, ref: HEAD)
    monkeypatch.setattr(upd, "apply_detached", lambda: pytest.fail("nothing to install: only a restart is due"))
    for arg in ("", "now"):
        out = (await UpdateCommand().handle(None, None, arg))["output"]
        assert f"current: {NEW}" in out and f"running: {OLD}" in out
        assert f"restart to use {NEW}" in out and "Already up to date" not in out


async def test_slash_update_on_a_release_install_says_to_restart_too(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(data, "0.9.0", "1.0.0")
    _point_current_at(data, "0.9.0")
    monkeypatch.setattr(sys, "prefix", str(data / "current" / "venv"))
    importlib.reload(_version)
    _point_current_at(data, "1.0.0")
    rel = upd.Release(tag="v1.0.0", version="1.0.0", body="notes", prerelease=False, assets={})
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: rel)
    monkeypatch.setattr(upd, "apply_detached", lambda: pytest.fail("nothing to install: only a restart is due"))
    out = (await UpdateCommand().handle(None, None, "now"))["output"]
    assert "running: 0.9.0" in out and "restart to use 1.0.0" in out and "Already up to date" not in out


async def test_slash_update_is_unchanged_when_the_running_build_is_current(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(data, NEW)
    (data / "versions" / NEW / ".ref").write_text("main\n")
    _point_current_at(data, NEW)
    monkeypatch.setattr(sys, "prefix", str(data / "current" / "venv"))
    importlib.reload(_version)
    monkeypatch.setattr(upd, "remote_head", lambda url, ref: HEAD)
    out = (await UpdateCommand().handle(None, None, ""))["output"]
    assert "Already up to date." in out and "running:" not in out and "restart" not in out


def test_update_check_names_the_build_the_daemon_still_runs(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`k3code update` is a fresh process, so the build to compare is the daemon's, read from its socket."""
    _install(data, NEW)
    (data / "versions" / NEW / ".ref").write_text("main\n")
    _point_current_at(data, NEW)

    async def daemon(sock=None):
        return doctor.Check("daemon", doctor.OK, "answering", data={"version": OLD})

    monkeypatch.setattr(doctor, "check_daemon", daemon)
    monkeypatch.setattr(upd, "remote_head", lambda url, ref: HEAD)
    r = CliRunner().invoke(cli, ["update", "--check"])
    assert r.exit_code == 0, r.output
    assert f"daemon:  {OLD} (restart to use {NEW}" in r.output and "Already up to date" not in r.output
    r = CliRunner().invoke(cli, ["update"])  # nothing to install: the restart line replaces "Already up to date."
    assert r.exit_code == 0, r.output
    assert f"daemon:  {OLD} (restart to use {NEW}" in r.output and "Already up to date" not in r.output


def test_update_check_is_quiet_when_the_daemon_runs_current(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(data, NEW)
    (data / "versions" / NEW / ".ref").write_text("main\n")
    _point_current_at(data, NEW)

    async def daemon(sock=None):
        return doctor.Check("daemon", doctor.OK, "answering", data={"version": NEW})

    monkeypatch.setattr(doctor, "check_daemon", daemon)
    monkeypatch.setattr(upd, "remote_head", lambda url, ref: HEAD)
    r = CliRunner().invoke(cli, ["update"])
    assert r.exit_code == 0, r.output
    assert "Already up to date." in r.output and "restart" not in r.output
