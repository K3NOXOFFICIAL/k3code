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


def _browser_with_tools(monkeypatch, tmp_path, ldd_out=None, apt=True, apt_knows=()):
    """An executable Chromium binary (an old-layout name) and a PATH holding only fake tools: ``ldd`` printing
    ``ldd_out`` (none when None), ``apt-get`` and ``apt-cache`` (knowing only the packages in ``apt_knows``)."""
    import importlib.util

    from k3code import doctor

    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "playwright" else None)
    shell_dir = tmp_path / "data" / "browsers" / "chromium_headless_shell-1" / "chrome-linux"
    shell_dir.mkdir(parents=True)
    binary = shell_dir / "headless_shell"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tools = {}
    if ldd_out is not None:
        tools["ldd"] = "".join(f"echo '{ln}'\n" for ln in ldd_out)
    if apt:
        tools["apt-get"] = "exit 0\n"
        tools["apt-cache"] = 'case "$2" in ' + "|".join(apt_knows or ("-",)) + ") exit 0 ;; *) exit 100 ;; esac\n"
    for name, body in tools.items():
        (bindir / name).write_text("#!/bin/sh\n" + body)
        (bindir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return doctor


NSPR_NSS_ALSA_MISSING = (
    "\tlinux-vdso.so.1 (0x00007ffd)",
    "\tlibnspr4.so => not found",
    "\tlibnss3.so => not found",
    "\tlibnssutil3.so => not found",
    "\tlibasound.so.2 => not found",
    "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f)",
)


def test_browser_check_names_the_libraries_a_chromium_binary_cannot_load(monkeypatch, tmp_path):
    """#113: the binary is there and executable but the loader cannot start it; the warning names the libraries
    and the apt packages, and does not send the user to the installer, which cannot fix it."""
    doctor = _browser_with_tools(monkeypatch, tmp_path, NSPR_NSS_ALSA_MISSING, apt_knows=("libasound2t64",))
    chk = doctor.check_browser()
    assert chk.status == doctor.WARN
    assert "cannot start" in chk.detail
    for lib in ("libnspr4.so", "libnss3.so", "libnssutil3.so", "libasound.so.2"):
        assert lib in chk.detail
    assert chk.fix == "sudo apt-get install -y libnspr4 libnss3 libasound2t64"
    assert "installer" not in chk.fix + chk.detail


def test_browser_check_picks_the_alsa_package_this_release_has(monkeypatch, tmp_path):
    """Ubuntu 24.04 renamed libasound2 to libasound2t64; older releases still ship libasound2."""
    doctor = _browser_with_tools(monkeypatch, tmp_path, ("\tlibasound.so.2 => not found",))
    assert doctor.check_browser().fix == "sudo apt-get install -y libasound2"


def test_browser_check_sends_other_missing_libraries_to_playwright_install_deps(monkeypatch, tmp_path):
    doctor = _browser_with_tools(monkeypatch, tmp_path, ("\tlibgbm.so.1 => not found", "\tlibnss3.so => not found"))
    chk = doctor.check_browser()
    assert chk.status == doctor.WARN and "libgbm.so.1" in chk.detail
    assert chk.fix.startswith("sudo ") and "-m playwright install-deps chromium-headless-shell" in chk.fix


def test_browser_check_without_apt_names_the_libraries_only(monkeypatch, tmp_path):
    doctor = _browser_with_tools(monkeypatch, tmp_path, NSPR_NSS_ALSA_MISSING, apt=False)
    chk = doctor.check_browser()
    assert chk.status == doctor.WARN and "libnspr4.so" in chk.detail
    assert "package" in chk.fix and "libnss3.so" in chk.fix and "apt-get" not in chk.fix


def test_browser_check_is_ok_when_ldd_finds_every_library(monkeypatch, tmp_path):
    doctor = _browser_with_tools(monkeypatch, tmp_path, ("\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f)",))
    assert doctor.check_browser().status == doctor.OK


def test_browser_check_without_ldd_cannot_tell_and_stays_ok(monkeypatch, tmp_path):
    doctor = _browser_with_tools(monkeypatch, tmp_path)
    assert doctor.check_browser().status == doctor.OK


def test_browser_check_finds_the_headless_shell_playwright_installs(monkeypatch, tmp_path):
    """#104: Playwright 1.63 names the binary chrome-headless-shell; the check only knew chrome and headless_shell, so
    every fresh install warned 'no executable browser' and told the user to re-run an installer that skips Chromium."""
    import importlib.util

    from k3code import doctor

    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "playwright" else None)
    shell_dir = tmp_path / "data" / "browsers" / "chromium_headless_shell-1243" / "chrome-headless-shell-linux64"
    shell_dir.mkdir(parents=True)
    chk = doctor.check_browser()
    assert chk.status == doctor.WARN and "re-run the installer" not in chk.fix  # the installer keeps the directory
    assert "playwright install --force --only-shell chromium" in chk.fix
    binary = shell_dir / "chrome-headless-shell"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    chk = doctor.check_browser()
    assert chk.status == doctor.OK and chk.data["exe"] == str(binary)


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


def test_doctor_names_chain_models_whose_context_window_is_a_guess():
    """Issue #30: an id with no models.<id>.context_window and no known family gets a hint; the others stay quiet."""
    from k3code.config import ProviderEntry, Settings
    from k3code.doctor import OK, WARN, check_context_windows

    gateway = ProviderEntry(
        name="gw",
        kind="openai",
        base_url="http://gw",
        api_key_env="K",
        models={"default": ["auto/coding-manual", "claude-sonnet-5-5"], "cheap": "set-explicitly"},
        tiers={"strong": ["gpt-4.1", "auto/coding-manual"]},
    )
    cfg = Settings(providers=[gateway], models={"set-explicitly": {"context_window": 64_000}})
    checks = check_context_windows(cfg)
    assert [(c.name, c.status) for c in checks] == [("context-window:auto/coding-manual", WARN)]
    assert "assuming 128000 tokens" in checks[0].detail
    assert "models: {auto/coding-manual: {context_window: <tokens>}}" in checks[0].fix

    cfg.models["auto/coding-manual"] = {"context_window": 1_000_000}
    assert [(c.name, c.status) for c in check_context_windows(cfg)] == [("context-window", OK)]


def test_context_window_check_says_so_when_no_chain_models_are_configured():
    from k3code.config import ProviderEntry, Settings
    from k3code.doctor import OK, check_context_windows

    for providers in ([], [ProviderEntry(name="cli", kind="claude-cli")]):
        checks = check_context_windows(Settings(providers=providers))
        assert [(c.name, c.status, c.detail) for c in checks] == [("context-window", OK, "no chain models configured")]


def test_doctor_clears_a_providers_auth_cooldown_when_its_key_answers(monkeypatch, tmp_path):
    """A rejected key arms a 5-minute+ auth cooldown; once the key answers a doctor probe, the entry must be usable."""
    import asyncio

    from k3code import doctor
    from k3code.config import ProviderEntry, Settings
    from k3code.router.classifier import FailoverReason
    from k3code.router.cooldown import CooldownStore

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    path = tmp_path / "cooldowns.json"
    seed = CooldownStore(path=path)
    for _ in range(2):  # the second rejection in a row arms the cooldown
        seed.arm(FailoverReason.auth, provider="good", model="m")
        seed.arm(FailoverReason.auth, provider="bad", model="m")

    async def fake_probe_key(p):
        return p.name == "good", 5.0, "HTTP 200" if p.name == "good" else "HTTP 401: rejected"

    monkeypatch.setattr(doctor, "_probe_key", fake_probe_key)
    entries = [
        ProviderEntry(name=n, kind="openai", base_url=f"http://{n}", api_key_env="K", api_key="k")
        for n in ("good", "bad")
    ]
    asyncio.run(doctor.check_providers(Settings(providers=entries)))
    left = CooldownStore(path=path)
    assert left.reason_of(provider="good", model="m") is None
    assert left.reason_of(provider="bad", model="m") is FailoverReason.auth


async def test_claude_cli_on_path_but_not_logged_in_is_a_warning(fake_claude):
    """`claude` being on PATH made doctor green; the first turn then failed 401 (issue #146)."""
    from k3code import doctor
    from k3code.config import ProviderEntry, Settings

    cfg = Settings(providers=[ProviderEntry(name="claude-cli", kind="claude-cli")])
    fake_claude("out")
    row = next(c for c in await doctor.check_providers(cfg) if c.name == "provider:claude-cli")
    assert row.status == doctor.WARN and "not logged in" in row.detail
    assert "`claude`" in row.fix and "/login" in row.fix
    for state in ("in", "old"):  # logged in, or a Claude Code that cannot say: no complaint
        fake_claude(state)
        row = next(c for c in await doctor.check_providers(cfg) if c.name == "provider:claude-cli")
        assert row.status == doctor.OK and "Claude Code CLI at" in row.detail, state
    fake_claude("out")
    row = next(c for c in await doctor.check_providers(cfg, probe=False) if c.name == "provider:claude-cli")
    assert row.status == doctor.OK  # --no-probe: claude is not run
