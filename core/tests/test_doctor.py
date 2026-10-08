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
    (tmp_path / "data" / "browsers" / "chromium-1").mkdir(parents=True)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "playwright" else None)
    chk = doctor.check_browser()
    assert chk.status == doctor.OK and "Playwright and Chromium" in chk.detail


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
