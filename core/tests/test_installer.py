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
    d.mkdir(parents=True, exist_ok=True)
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


def test_update_flags_print_only_the_version(tmp_path: Path) -> None:
    # k3code update runs the installer with these flags and reads the version from stdout: nothing else may land there.
    r = run(tmp_path, INSTALL, "--from-source", "--yes", "--no-setup", "--no-activate", "--print-version")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith((REPO / "VERSION").read_text().strip())
    assert "presetup" not in r.stderr  # presetup runs only after activation


def test_presetup_second_run_changes_nothing(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--yes").returncode == 0
    before = snapshot(tmp_path)
    again = run(tmp_path, INSTALL, "--from-source", "--yes")
    assert again.returncode == 0, again.stderr
    assert "presetup (optional" in again.stderr  # the phase still runs and reports
    assert snapshot(tmp_path) == before


def test_bwrap_missing_prints_the_command_and_exits_zero(tmp_path: Path) -> None:
    sudo_log = tmp_path / "sudo.log"
    sudo = stub_bin(tmp_path, "sudo", f'echo "sudo $*" >>"{sudo_log}"\nexit 1\n')
    r = run(tmp_path, INSTALL, "--from-source", "--yes", env_extra={"K3_BWRAP": "/nonexistent/bwrap"}, path_front=sudo)
    assert r.returncode == 0, r.stderr
    assert "bubblewrap is not installed" in r.stderr
    assert "install it:" in r.stderr
    assert not sudo_log.exists()  # never sudo unattended, not even when the command is known


def test_bwrap_installed_but_unusable_is_a_warning(tmp_path: Path) -> None:
    bwrap = stub_bin(tmp_path, "bwrap", "exit 1\n")
    r = run(tmp_path, INSTALL, "--from-source", "--yes", env_extra={"K3_BWRAP": str(bwrap / "bwrap")})
    assert r.returncode == 0, r.stderr
    assert "installed but cannot create a sandbox" in r.stderr


def test_bwrap_usable_is_reported_ok(tmp_path: Path) -> None:
    bwrap = stub_bin(tmp_path, "bwrap", "exit 0\n")
    r = run(tmp_path, INSTALL, "--from-source", "--yes", env_extra={"K3_BWRAP": str(bwrap / "bwrap")})
    assert r.returncode == 0, r.stderr
    assert "sandbox ok" in r.stderr


def _source_repo(root: Path) -> Path:
    """A minimal k3code-shaped git repository (VERSION, core/pyproject.toml) with a tag v0.0.1 and branch main."""
    src = root / "srcrepo"
    (src / "core").mkdir(parents=True)
    (src / "VERSION").write_text("0.0.1\n")
    (src / "core" / "pyproject.toml").write_text("[project]\nname = 'k3code'\nversion = '0.0.1'\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(src)]
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(src)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    subprocess.run([*git, "tag", "v0.0.1"], check=True)
    return src


def test_from_git_tag_reruns_without_network(tmp_path: Path) -> None:
    src = _source_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    first = run(home, INSTALL, "--yes", "--from-git", f"file://{src}", "--ref", "v0.0.1")
    assert first.returncode == 0, first.stderr
    # the remote is gone now: a complete tag install must not touch it
    gone = f"file://{tmp_path / 'gone.git'}"
    again = run(home, INSTALL, "--yes", "--from-git", gone, "--ref", "v0.0.1")
    assert again.returncode == 0, again.stderr
    assert "already installed" in again.stderr
    assert "fetching" not in again.stderr


def test_from_git_branch_falls_back_to_the_installed_build_when_offline(tmp_path: Path) -> None:
    src = _source_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    assert run(home, INSTALL, "--yes", "--from-git", f"file://{src}", "--ref", "main").returncode == 0
    gone = f"file://{tmp_path / 'gone.git'}"
    again = run(home, INSTALL, "--yes", "--from-git", gone, "--ref", "main")
    assert again.returncode == 0, again.stderr
    assert "could not fetch 'main'" in again.stderr
    assert "Using the installed version" in again.stderr
    assert (home / ".local" / "bin" / "k3code").is_symlink()


def test_fetch_errors_separate_no_network_from_private_repo(tmp_path: Path) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    offline = "fatal: unable to access 'https://example.invalid/x.git/': Could not resolve host: example.invalid"
    private = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
    cases = {offline: "no network", private: "private or needs credentials"}
    for i, (text, phrase) in enumerate(cases.items()):
        body = f'case "$*" in *fetch*) echo "{text}" >&2; exit 128 ;; esac\nexec {real_git} "$@"\n'
        stubs = stub_bin(tmp_path / f"case{i}", "git", body)
        home = tmp_path / f"home{i}"
        home.mkdir()
        r = run(
            home, INSTALL, "--yes", "--from-git", "https://example.invalid/x.git", "--ref", "main", path_front=stubs
        )
        assert r.returncode != 0
        assert "could not fetch 'main'" in r.stderr
        assert phrase in r.stderr, r.stderr


def test_uv_temp_file_is_removed_when_the_uv_download_fails(tmp_path: Path) -> None:
    # uv is only found in ~/.local/bin here: drop that PATH entry so the installer tries to fetch uv itself
    path = os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / "uv").exists())
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    curl = stub_bin(tmp_path, "curl", "exit 22\n")
    home = tmp_path / "home"
    home.mkdir()
    r = run(
        home,
        INSTALL,
        "--from-source",
        "--yes",
        env_extra={"PATH": path, "TMPDIR": str(tmpdir)},
        drop=("K3_NO_DOWNLOAD",),  # that flag means --no-install-deps, which never downloads uv
        path_front=curl,
    )
    assert r.returncode != 0
    assert "could not download the uv installer" in r.stderr
    assert list(tmpdir.glob("uv-install.*")) == []


def test_check_reports_the_node_floor_of_20(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--check")
    assert r.returncode == 0, r.stderr
    assert "node 20+ with npm" in r.stderr
    assert "node 18" not in r.stderr


def test_uninstall_removes_presetup_leftovers_and_keeps_user_data(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--yes").returncode == 0
    data = tmp_path / ".local" / "share" / "k3code"
    (data / "presetup").mkdir(exist_ok=True)
    (data / "presetup" / "chromium-marker").write_text("ok\n")
    (data / "browsers" / "chromium-1").mkdir(parents=True, exist_ok=True)
    (data / "browsers" / "chromium-1" / "chrome").write_text("binary\n")
    (tmp_path / ".k3code").mkdir(exist_ok=True)
    (tmp_path / ".k3code" / "config.yaml").write_text("x: 1\n")
    (tmp_path / ".config" / "k3code").mkdir(parents=True)
    (tmp_path / ".config" / "k3code" / "env").write_text("EXAMPLE_NAME=placeholder\n")

    r = run(tmp_path, UNINSTALL)
    assert r.returncode == 0, r.stderr
    assert not data.exists()  # presetup markers and the browser location go with the install
    assert (tmp_path / ".k3code" / "config.yaml").is_file()
    assert (tmp_path / ".config" / "k3code" / "env").is_file()
    assert run(tmp_path, UNINSTALL, "--purge").returncode == 0
    assert not (tmp_path / ".k3code").exists()
    assert not (tmp_path / ".config" / "k3code").exists()


def test_presetup_shows_the_doctor_subset_and_ignores_its_status(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--from-source", "--yes")
    assert r.returncode == 0, r.stderr  # the stub doctor exits 1; the install still succeeds
    assert "health subset" in r.stderr
    assert "browser: stub" in r.stderr


CHROMIUM_MARKER = Path("presetup") / "chromium-1.63.0"


def test_chromium_is_on_by_default_at_a_version_independent_location(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--from-source", "--yes")
    assert r.returncode == 0, r.stderr
    data = tmp_path / ".local" / "share" / "k3code"
    assert (data / CHROMIUM_MARKER).is_file()
    assert "Chromium stub" in r.stderr
    # outside versions/: an update prunes old versions but keeps the browser
    assert not any(p.name == "chromium-1.63.0" for p in (data / "versions").rglob("*"))


def test_chromium_opt_outs_write_no_marker(tmp_path: Path) -> None:
    skipped = tmp_path / "skip"
    skipped.mkdir()
    r = run(skipped, INSTALL, "--from-source", "--yes", env_extra={"K3CODE_SKIP_CHROMIUM": "1"})
    assert r.returncode == 0, r.stderr
    assert "Chromium skipped" in r.stderr
    assert not (skipped / ".local" / "share" / "k3code" / CHROMIUM_MARKER).exists()

    minimal = tmp_path / "minimal"
    minimal.mkdir()
    m = run(minimal, INSTALL, "--from-source", "--yes", "--minimal")
    assert m.returncode == 0, m.stderr
    assert not (minimal / ".local" / "share" / "k3code" / CHROMIUM_MARKER).exists()


def test_uninstall_removes_the_chromium_location(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--yes").returncode == 0
    data = tmp_path / ".local" / "share" / "k3code"
    assert (data / CHROMIUM_MARKER).is_file()
    assert run(tmp_path, UNINSTALL).returncode == 0
    assert not data.exists()


def test_presetup_doctor_sees_a_custom_prefix_data_dir(tmp_path: Path) -> None:
    # with --prefix the browser location is under that prefix, so the doctor must be told the data dir explicitly
    prefix = tmp_path / "custom"
    r = run(tmp_path, INSTALL, "--from-source", "--yes", "--prefix", str(prefix))
    assert r.returncode == 0, r.stderr
    assert f"data={prefix / 'share' / 'k3code'}" in r.stderr
||||||| 8d7de67
def test_unit_files_share_one_restart_budget_and_a_recovery_unit(tmp_path, monkeypatch):
    """P1-2: the repo copies, the rendered unit and the recovery unit agree; systemd gives up one start after the
    daemon's own storm guard (safe mode), and the recovery unit starts the daemon again after a cooldown."""
    from k3code import daemon, service

    main_repo = (REPO / "install" / "systemd" / "k3code.service").read_text()
    recover_repo = (REPO / "install" / "systemd" / "k3code-recover.service").read_text()
    policy = ("StartLimitIntervalSec=", "StartLimitBurst=", "Restart=", "RestartSec=", "OnFailure=")

    def lines(text: str) -> list[str]:
        return [ln for ln in text.splitlines() if ln.startswith(policy)]

    assert lines(main_repo) == lines(service.render_unit("/usr/bin/k3code daemon"))
    assert service.START_LIMIT_BURST == daemon.RESTART_LIMIT + 1
    assert service.START_LIMIT_INTERVAL_S == int(daemon.RESTART_WINDOW_S)  # noqa: SIM300
    assert "OnFailure=k3code-recover.service" in main_repo
    assert recover_repo == service.render_recover_unit()
    assert "reset-failed k3code.service" in recover_repo and "systemctl --user start k3code.service" in recover_repo

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(service, "_systemctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))  # no real manager
    service.install()
    recover = tmp_path / "cfg" / "systemd" / "user" / "k3code-recover.service"
    assert recover.read_text() == service.render_recover_unit()
    service.uninstall()
    assert not recover.exists()
