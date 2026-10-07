

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
