def test_api_key_check_accepts_a_key_loaded_from_the_env_file(monkeypatch):
    """The wizard stores keys in ~/.config/k3code/env; load_config fills p.api_key from it. check_keys only looked at
    os.environ, so `k3code doctor` and the update smoke test failed in every shell that did not export the key."""
    from k3code.config import ProviderEntry, Settings
    from k3code.doctor import FAIL, OK, check_keys

    monkeypatch.delenv("SOME_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("K3CODE_FAKE_PROVIDER", raising=False)
    entry = {"name": "p", "kind": "openai", "base_url": "http://p", "api_key_env": "SOME_PROVIDER_KEY"}
    have = Settings(providers=[ProviderEntry(**entry, api_key="from-env-file")])
    assert check_keys(have).status == OK
    lack = Settings(providers=[ProviderEntry(**entry)])
    assert check_keys(lack).status == FAIL


def test_sandbox_warns_when_bwrap_is_installed_but_unusable(monkeypatch):
    """bwrap present with user namespaces blocked used to report OK, while every sandboxed command failed."""
    from k3code import doctor
    from k3code.reliability import sandbox

    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "usable", lambda: False)
    chk = doctor.check_sandbox()
    assert chk.status == doctor.WARN
    assert chk.detail.startswith("bubblewrap is installed but unusable (user namespaces blocked?)")
    assert "unattended bash is refused" in chk.detail


def test_sandbox_is_ok_when_bwrap_is_usable(monkeypatch):
    from k3code import doctor
    from k3code.reliability import sandbox

    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "usable", lambda: True)
    chk = doctor.check_sandbox()
    assert chk.status == doctor.OK and "/usr/bin/bwrap" in chk.detail


def test_sandbox_no_probe_does_not_run_bwrap(monkeypatch):
    from k3code import doctor
    from k3code.reliability import sandbox

    def must_not_probe():
        raise AssertionError("--no-probe must not spawn bwrap")

    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "usable", must_not_probe)
    assert doctor.check_sandbox(probe=False).status == doctor.OK


def test_install_subset_never_fails_and_covers_browser_and_searxng(monkeypatch):
    """Warnings only: a fresh install (no providers, no daemon, no node) must still report every check as ok or warn."""
    from k3code import doctor

    monkeypatch.setattr(doctor, "check_tui", lambda: doctor.Check("tui", doctor.FAIL, "node not found", "install node"))
    checks = doctor.install_subset()
    names = {c.name for c in checks}
    assert {"browser", "searxng", "k3code-home", "disk", "tui"} <= names
    assert "providers" not in names and "daemon" not in names
    assert all(c.status in (doctor.OK, doctor.WARN) for c in checks)
    assert next(c for c in checks if c.name == "tui").status == doctor.WARN


def test_browser_check_reports_playwright_and_chromium(monkeypatch, tmp_path):
    import importlib.util

    from k3code import doctor

    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert doctor.check_browser().status == doctor.WARN
    shell_dir = tmp_path / "data" / "browsers" / "chromium_headless_shell-1" / "chrome-linux"
    shell_dir.mkdir(parents=True)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "playwright" else None)
    chk = doctor.check_browser()  # the directory exists but holds no browser
    assert chk.status == doctor.WARN and "no executable browser" in chk.detail
    binary = shell_dir / "headless_shell"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o644)  # present but not executable
    assert doctor.check_browser().status == doctor.WARN
    binary.chmod(0o755)
    chk = doctor.check_browser()
    assert chk.status == doctor.OK and "Playwright and Chromium" in chk.detail


def test_env_file_check_flags_a_loose_mode_and_export_lines(monkeypatch, tmp_path):
    from k3code import doctor

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert doctor.check_env_file().status == doctor.OK  # no file: nothing to say
    env = tmp_path / "k3code" / "env"
    env.parent.mkdir()
    env.write_text("K3CODE_API_KEY=fake\n")
    env.chmod(0o644)
    chk = doctor.check_env_file()
    assert chk.status == doctor.WARN and f"chmod 600 {env}" in chk.fix
    env.chmod(0o600)
    assert doctor.check_env_file().status == doctor.OK
    env.write_text("K3CODE_API_KEY=fake\nexport OMNIROUTE_API_KEY=fake\n")
    chk = doctor.check_env_file()
    assert chk.status == doctor.WARN and "rejects 'export'" in chk.detail and "line 2" in chk.detail
    assert "fake" not in chk.detail + chk.fix  # values are never echoed


def test_linger_check_warns_only_for_an_installed_unit_with_lingering_off(monkeypatch, tmp_path):
    import subprocess

    from k3code import doctor, service

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert doctor.check_linger().status == doctor.OK  # no unit installed
    service.unit_path().parent.mkdir(parents=True)
    service.unit_path().write_text("[Service]\n")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/loginctl")
    answer = {"out": "Linger=no\n"}
    monkeypatch.setattr(
        doctor.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=answer["out"], stderr="")
    )
    chk = doctor.check_linger()
    assert chk.status == doctor.WARN and "loginctl enable-linger" in chk.fix and "stops at logout" in chk.detail
    answer["out"] = "Linger=yes\n"
    assert doctor.check_linger().status == doctor.OK
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # no loginctl: cannot tell, no warning
    assert doctor.check_linger().status == doctor.OK


def test_daemon_version_check_compares_with_the_current_symlink(monkeypatch, tmp_path):
    from k3code import doctor

    data = tmp_path / "data"
    (data / "versions" / "1.2.3-src.abc").mkdir(parents=True)
    (data / "versions" / "1.2.4").mkdir()
    monkeypatch.setenv("K3CODE_DATA", str(data))
    assert doctor.check_daemon_version("1.2.3").status == doctor.OK  # no `current`: not comparable
    (data / "current").symlink_to(data / "versions" / "1.2.4")
    assert doctor.check_daemon_version(None).status == doctor.OK  # daemon not running
    assert doctor.check_daemon_version("1.2.4").status == doctor.OK
    chk = doctor.check_daemon_version("1.2.3")
    assert chk.status == doctor.WARN and "restart the daemon" in chk.fix
    (data / "current").unlink()
    (data / "current").symlink_to(data / "versions" / "1.2.3-src.abc")
    assert doctor.check_daemon_version("1.2.3").status == doctor.OK  # "<version>-src.<sha>" dirs match


def test_install_links_check_flags_a_dangling_current_and_a_missing_previous(monkeypatch, tmp_path):
    from k3code import doctor

    data = tmp_path / "data"
    (data / "versions" / "1.0.0").mkdir(parents=True)
    monkeypatch.setenv("K3CODE_DATA", str(data))
    assert doctor.check_install_links().status == doctor.OK  # nothing installed
    (data / "current").symlink_to(data / "versions" / "0.9.0")  # target does not exist
    chk = doctor.check_install_links()
    assert chk.status == doctor.WARN and "missing" in chk.detail
    (data / "current").unlink()
    (data / "current").symlink_to(data / "versions" / "1.0.0")
    assert doctor.check_install_links().status == doctor.OK
    (data / "previous").write_text("0.9.0\n")
    chk = doctor.check_install_links()
    assert chk.status == doctor.WARN and "rollback" in chk.detail
    (data / "versions" / "0.9.0").mkdir()
    assert doctor.check_install_links().status == doctor.OK


async def test_check_daemon_reports_the_running_version(tmp_path):
    import asyncio

    from k3code import __version__, doctor

    sock = tmp_path / "d.sock"

    async def handle(reader, writer):
        await reader.readline()
        writer.write(
            b'{"jsonrpc":"2.0","id":"doctor","result":{"ready":true,"version":"' + __version__.encode() + b'"}}\n'
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock))
    try:
        chk = await doctor.check_daemon(sock)
    finally:
        server.close()
    assert chk.status == doctor.OK and chk.data["version"] == __version__


def test_searxng_check_reads_the_research_setting(monkeypatch, tmp_path):
    from k3code import doctor

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    assert "not configured" in doctor.check_searxng().detail
    (tmp_path / "home" / "config.yaml").write_text("research:\n  searxng_url: https://search.example.invalid\n")
    assert "search.example.invalid" in doctor.check_searxng().detail


def test_doctor_install_flag_exits_zero_with_no_providers(monkeypatch):
    from click.testing import CliRunner

    from k3code.cli import cli

    result = CliRunner().invoke(cli, ["doctor", "--install"])
    assert result.exit_code == 0, result.output
    assert "browser" in result.output and "searxng" in result.output


def test_a_missing_key_behind_a_working_entry_is_a_warning_not_a_failure(monkeypatch):
    """claude-cli first, a keyless gateway as fallback: the chain still runs, so doctor must not report a failure."""
    from k3code.config import ProviderEntry, Settings
    from k3code.doctor import FAIL, WARN, check_keys

    monkeypatch.delenv("SOME_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("K3CODE_FAKE_PROVIDER", raising=False)
    gateway = ProviderEntry(name="gw", kind="openai", base_url="http://gw", api_key_env="SOME_PROVIDER_KEY")
    chain = Settings(providers=[ProviderEntry(name="claude-cli", kind="claude-cli"), gateway])
    chk = check_keys(chain)
    assert chk.status == WARN and "SOME_PROVIDER_KEY" in chk.detail
    assert check_keys(Settings(providers=[gateway])).status == FAIL


async def test_a_keyless_provider_that_answers_401_is_not_reported_ok(monkeypatch):
    """Without a key doctor only probes reachability; HTTP 401 there showed as a green check."""
    from k3code import doctor
    from k3code.config import ProviderEntry, Settings

    monkeypatch.delenv("SOME_PROVIDER_KEY", raising=False)

    async def reachable(base_url, timeout=5.0):
        return True, 12.0, "HTTP 401"

    monkeypatch.setattr(doctor, "_probe_provider", reachable)
    gateway = ProviderEntry(name="gw", kind="openai", base_url="http://gw", api_key_env="SOME_PROVIDER_KEY")
    checks = await doctor.check_providers(Settings(providers=[gateway]))
    row = next(c for c in checks if c.name == "provider:gw")
    assert row.status == doctor.WARN
    assert "no key in SOME_PROVIDER_KEY" in row.detail and "setup --step providers" in row.fix


def test_the_key_count_leaves_out_claude_cli(monkeypatch):
    from k3code.config import ProviderEntry, Settings
    from k3code.doctor import OK, check_keys

    gateway = ProviderEntry(name="gw", kind="openai", base_url="http://gw", api_key_env="K", api_key="x")
    chk = check_keys(Settings(providers=[gateway, ProviderEntry(name="claude-cli", kind="claude-cli")]))
    assert (chk.status, chk.detail) == (OK, "1 key env var(s) present")
    assert (
        check_keys(Settings(providers=[ProviderEntry(name="c", kind="claude-cli")])).detail == "no provider needs a key"
    )


def test_project_check_reports_the_stacks_a_read_only_scan_finds(tmp_path):
    """A fresh uv + pnpm project read "project: none" (the trust state of a project without a config) until a TUI
    session had scanned it; doctor now scans it itself and stores nothing."""
    from k3code import doctor
    from k3code.learning import projectstate

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0"\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / "package.json").write_text('{"name": "web"}\n')
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    chk = doctor.check_project(tmp_path)
    assert chk.status == doctor.OK
    assert chk.detail.startswith("stacks: python (uv), node (pnpm); no project config")
    assert chk.data["stacks"] == ["python (uv)", "node (pnpm)"]
    assert not projectstate.state_path(tmp_path).exists()
