

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
