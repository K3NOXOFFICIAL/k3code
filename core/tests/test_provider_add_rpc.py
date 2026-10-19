"""The /provider popup's add form (gateway side): ``provider.presets``, ``provider.probe`` and ``provider.add``.

``provider.add`` writes the block the way ``/model chain add`` does and the key the way the setup wizard does (the
0600 env file under the block's ``api_key_env``), never into config.yaml, an error or the log."""

from __future__ import annotations

import logging
import stat

import pytest
import yaml

import m1cmd_helpers as m1
from k3code import chain_config
from k3code.config import ProviderEntry
from k3code.setup import probe
from k3code.setup.state import env_file_path, read_env_file, set_env_var

KEY = "-".join(("sk", "add", "0123456789abcdef"))  # built at run time: no key-like literal (gitleaks)
OLD = "-".join(("sk", "old", "fedcba9876543210"))


@pytest.fixture
async def server(tmp_path, monkeypatch):
    for spec in probe.PRESETS.values():
        monkeypatch.delenv(spec["api_key_env"], raising=False)
    monkeypatch.delenv("K3CODE_MINE_API_KEY", raising=False)
    srv, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    path = chain_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    first = {"name": "first", "kind": "openai", "base_url": "http://first/v1", "api_key_env": "FIRST_KEY"}
    path.write_text(yaml.safe_dump({"providers": [{**first, "models": {"default": "m1"}}]}))
    srv.config.providers = [ProviderEntry(**first, models={"default": "m1"})]
    monkeypatch.chdir(tmp_path)  # reload_providers reads the project config of the cwd: none here
    assert str(env_file_path()).startswith(str(tmp_path.parent))  # never the real ~/.config
    yield srv
    await srv.close()


async def _ok(server, method: str, params: dict) -> dict:
    res = await m1.rpc(server, method, params)
    assert "result" in res, res
    return res["result"]


async def _err(server, method: str, params: dict) -> str:
    res = await m1.rpc(server, method, params)
    assert "error" in res, res
    assert res["error"]["code"] == -32602
    return res["error"]["message"]


def _add(**over) -> dict:
    return {
        "preset": "openai",
        "name": "mine",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": KEY,
        "model": "gpt-5.1",
    } | over


async def test_presets_come_from_the_setup_wizard_with_key_state_but_no_key(server):
    set_env_var("OPENAI_API_KEY", OLD)
    rows = {r["id"]: r for r in (await _ok(server, "provider.presets", {}))["presets"]}
    assert list(rows) == [*probe.PRESETS, "custom", "custom-anthropic"]
    assert rows["openai"]["key_set"] is True and rows["anthropic"]["key_set"] is False
    assert rows["local"]["keyless"] is True and rows["custom-anthropic"]["kind"] == "anthropic"
    assert OLD not in str(rows)


async def test_add_writes_the_block_and_a_0600_key_never_the_key_in_config(server, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    res = await _ok(server, "provider.add", _add())
    assert res["key_env"] == "OPENAI_API_KEY"  # the preset's own variable, free here
    cfg = chain_config.config_path().read_text()
    assert KEY not in cfg
    block = yaml.safe_load(cfg)["providers"][-1]
    assert block == {
        "name": "mine",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "models": {"default": "gpt-5.1"},
    }
    assert read_env_file() == {"OPENAI_API_KEY": KEY}
    assert stat.S_IMODE(env_file_path().stat().st_mode) == 0o600
    live = {p.name: p for p in server.config.providers}
    assert list(live) == ["first", "mine"] and live["mine"].api_key == KEY  # reloaded like /model chain add
    assert server._chain_key is None  # the router is rebuilt on the next turn
    assert res["provider"]["slug"] == "mine"
    assert KEY not in str(res) and KEY not in caplog.text


async def test_a_new_key_never_replaces_a_key_already_stored(server):
    set_env_var("OPENAI_API_KEY", OLD)
    res = await _ok(server, "provider.add", _add())
    assert res["key_env"] == "K3CODE_MINE_API_KEY"
    assert read_env_file() == {"OPENAI_API_KEY": OLD, "K3CODE_MINE_API_KEY": KEY}


async def test_reuse_keeps_the_preset_variable_and_only_at_the_vendor_url(server):
    set_env_var("OPENAI_API_KEY", OLD)
    res = await _ok(server, "provider.add", _add(api_key=None, reuse_key=True))
    assert res["key_env"] == "OPENAI_API_KEY" and read_env_file() == {"OPENAI_API_KEY": OLD}
    msg = await _err(server, "provider.add", _add(name="x", base_url="https://evil.example/v1", reuse_key=True))
    assert "own vendor's URL" in msg and OLD not in msg


async def test_keyless_local_preset(server):
    params = _add(preset="local", name="ollama", base_url="http://localhost:11434/v1", api_key="", model="llama3")
    res = await _ok(server, "provider.add", params)
    assert res["key_env"] == ""
    assert yaml.safe_load(chain_config.config_path().read_text())["providers"][-1]["api_key_env"] == ""
    assert read_env_file() == {}


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"name": "First"}, "a provider named First already exists"),
        ({"name": "first"}, "a provider named first already exists"),
        ({"base_url": "ftp://host/v1"}, "base URL must start with http:// or https://"),
        ({"base_url": "not a url"}, "base URL must start with http:// or https://"),
        ({"name": "has space"}, "name: a letter"),
        ({"model": ""}, "model: one model id"),
        ({"api_key": ""}, "an API key is needed"),
        ({"kind": "claude-cli"}, "kind must be openai or anthropic"),
    ],
)
async def test_bad_requests_are_clean_errors_and_write_nothing(server, caplog, over, message):
    before = chain_config.config_path().read_text()
    caplog.set_level(logging.DEBUG)
    msg = await _err(server, "provider.add", _add(**over))
    assert message in msg and KEY not in msg and KEY not in caplog.text
    assert chain_config.config_path().read_text() == before and read_env_file() == {}
    assert [p.name for p in server.config.providers] == ["first"]


async def test_a_failed_config_write_removes_the_stored_key(server, monkeypatch):
    def boom(*_a, **_k):
        raise chain_config.ChainEditError(f"disk full {KEY[:0]}")

    monkeypatch.setattr(chain_config, "edit_config", boom)
    msg = await _err(server, "provider.add", _add())
    assert "could not add mine: disk full" in msg and KEY not in msg
    assert read_env_file() == {}


async def test_probe_lists_models_with_the_key_and_suggests_a_default(server, monkeypatch):
    seen = {}

    def fake(entry, key, timeout=8.0):
        seen.update(entry=entry, key=key)
        return True, 12.0, ["gpt-5.1", "gpt-4o"], "HTTP 200, 2 models"

    monkeypatch.setattr(probe, "list_models", fake)
    res = await _ok(server, "provider.probe", _add())
    assert res == {
        "ok": True,
        "ms": 12,
        "detail": "HTTP 200, 2 models",
        "hint": "",
        "models": ["gpt-5.1", "gpt-4o"],
        "suggested": "gpt-5.1",
    }
    assert seen == {"entry": {"kind": "openai", "base_url": "https://api.openai.com/v1"}, "key": KEY}
    monkeypatch.setattr(probe, "list_models", lambda e, k, timeout=8.0: (False, 3.0, [], "HTTP 401"))
    res = await _ok(server, "provider.probe", _add())
    assert res["ok"] is False and res["hint"] == "the endpoint rejected the key" and KEY not in str(res)
