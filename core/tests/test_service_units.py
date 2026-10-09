"""The systemd user units: the repo copies are the rendered templates, the daemon runs with the user's umask, the
recovery unit is hardened like the daemon, and ``service uninstall`` removes both units."""

from __future__ import annotations

import subprocess
from pathlib import Path

from k3code import service

REPO = Path(__file__).resolve().parents[2]
HARDENING = ("NoNewPrivileges=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes", "RestrictRealtime=yes")


def directives(text: str) -> dict[str, str]:
    return dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln and not ln.startswith("#"))


def test_repo_units_are_the_rendered_templates() -> None:
    main_repo = (REPO / "install" / "systemd" / "k3code.service").read_text()
    recover_repo = (REPO / "install" / "systemd" / "k3code-recover.service").read_text()
    assert main_repo == service.render_unit(service.DEFAULT_EXEC_START)
    assert recover_repo == service.render_recover_unit()


def test_the_daemon_writes_with_the_users_umask() -> None:
    # UMask=0077 made every project file a tool wrote 0600 and every new directory 0700
    assert directives(service.render_unit("/usr/bin/k3code daemon"))["UMask"] == "0022"


def test_the_recovery_unit_is_hardened_like_the_daemon() -> None:
    main = service.render_unit("/usr/bin/k3code daemon")
    recover = service.render_recover_unit()
    for line in HARDENING:
        assert line in main.splitlines() and line in recover.splitlines(), line
    d = directives(recover)
    assert d["UMask"] == "0022"
    assert "MemoryMax" in d and "TasksMax" in d


def test_uninstall_stops_and_removes_the_recovery_unit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    calls: list[tuple[str, ...]] = []

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_systemctl", fake)
    service.install()
    calls.clear()
    out = service.uninstall()
    assert not service.unit_path().exists() and not service.recover_unit_path().exists()
    # a sleeping recovery unit would start the daemon again after the uninstall: it is stopped first
    stop = calls.index(("stop", service.RECOVER_UNIT_NAME))
    assert stop < calls.index(("disable", "--now", service.UNIT_NAME))
    assert calls[-1] == ("daemon-reload",)
    assert any(service.RECOVER_UNIT_NAME in line for line in out)
    assert any(service.RECOVER_UNIT_NAME in step for step in service.uninstall(dry_run=True))


def test_install_dry_run_prints_every_unit_it_would_write(tmp_path: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from k3code.cli import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    r = CliRunner().invoke(cli, ["service", "install", "--dry-run"])
    assert r.exit_code == 0, r.output
    # both unit texts, not only their names in the step list
    assert service.render_unit() in r.output
    assert service.render_recover_unit() in r.output
    assert f"unit file {service.recover_unit_path()}:" in r.output
    assert not service.unit_path().exists() and not service.recover_unit_path().exists()
