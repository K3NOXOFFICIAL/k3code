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
from k3code import config as config_mod
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
    monkeypatch.delenv("K3CODE_PROJECT_DIR", raising=False)
    srv, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    path = chain_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    first = {"name": "first", "kind": "openai", "base_url": "http://first/v1", "api_key_env": "FIRST_KEY"}
    path.write_text(yaml.safe_dump({"providers": [{**first, "models": {"default": "m1"}}]}))
    srv.config.providers = [ProviderEntry(**first, models={"default": "m1"})]
    monkeypatch.chdir(tmp_path)  # reload_providers reads the project config of the cwd (no K3CODE_PROJECT_DIR): none
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
        ({"base_url": "http://[::1/v1"}, "base URL must start with http:// or https://"),
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
        raise chain_config.ChainEditError(f"disk full {KEY}")

    monkeypatch.setattr(chain_config, "edit_config", boom)
    msg = await _err(server, "provider.add", _add())
    assert "could not add mine: disk full" in msg and KEY not in msg
    assert read_env_file() == {}


async def test_probe_lists_models_with_the_key_and_suggests_a_default(server, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
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
    assert KEY not in caplog.text


async def test_a_key_for_an_edited_url_never_takes_the_vendor_variable(server, monkeypatch):
    res = await _ok(server, "provider.add", _add(base_url="https://proxy.example/v1"))
    assert res["key_env"] == "K3CODE_MINE_API_KEY"  # OPENAI_API_KEY is free, but holds only OpenAI's key
    assert read_env_file() == {"K3CODE_MINE_API_KEY": KEY}
    rows = {r["id"]: r for r in (await _ok(server, "provider.presets", {}))["presets"]}
    assert rows["openai"]["key_set"] is False
    sent = []
    monkeypatch.setattr(probe, "list_models", lambda e, k, timeout=8.0: sent.append(k) or (True, 1.0, [], ""))
    msg = await _err(server, "provider.probe", _add(api_key=None, reuse_key=True))  # at api.openai.com
    assert "OPENAI_API_KEY holds no key to reuse" in msg and sent == []


@pytest.mark.parametrize("method", ["provider.add", "provider.probe"])
async def test_a_url_with_a_user_or_password_is_refused_and_never_echoed(server, monkeypatch, caplog, method):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(probe, "list_models", lambda *_a, **_k: pytest.fail("probed"))
    before = chain_config.config_path().read_text()
    for url in ("https://bob:hunter2pass@gw.example/v1", "https://hunter2pass@gw.example/v1"):
        msg = await _err(server, method, _add(preset="custom", base_url=url))
        assert "must not carry a user name or password" in msg and "hunter2pass" not in msg
    assert "hunter2pass" not in caplog.text
    assert chain_config.config_path().read_text() == before and read_env_file() == {}


@pytest.mark.parametrize("method", ["provider.add", "provider.probe"])
async def test_a_url_with_a_query_or_fragment_is_refused_and_never_echoed(server, monkeypatch, caplog, method):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(probe, "list_models", lambda *_a, **_k: pytest.fail("probed"))
    before = chain_config.config_path().read_text()
    secret = "QSECRET" + "0123456789"
    for url in (f"https://gw.example/v1?api_key={secret}", f"https://gw.example/v1#{secret}", "https://gw.example/v1?"):
        msg = await _err(server, method, _add(preset="custom", base_url=url))
        assert "must not carry a query string or fragment" in msg and secret not in msg and "gw.example" not in msg
    assert secret not in caplog.text
    assert chain_config.config_path().read_text() == before and read_env_file() == {}


@pytest.mark.parametrize("exc", [RuntimeError("owned by another user"), ValueError("bad")])
async def test_any_failure_of_the_config_write_removes_the_stored_key(server, monkeypatch, exc):
    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(chain_config, "edit_config", boom)
    msg = await _err(server, "provider.add", _add())
    assert msg == f"could not add mine: {type(exc).__name__}" and KEY not in msg
    assert read_env_file() == {}
    assert [p.name for p in server.config.providers] == ["first"]


async def test_a_failed_reload_after_the_write_reports_the_add_with_a_warning_and_keeps_the_key(
    server, monkeypatch, caplog
):
    def boom(_ctx):
        raise RuntimeError(f"reload failed {KEY}")

    monkeypatch.setattr(chain_config, "reload_providers", boom)
    caplog.set_level(logging.DEBUG)
    res = await _ok(server, "provider.add", _add())
    assert KEY not in caplog.text and "reload failed" in caplog.text
    assert res["warning"] == "providers not reloaded: RuntimeError; run /reload" and KEY not in str(res)
    assert res["key_env"] == "OPENAI_API_KEY" and read_env_file() == {"OPENAI_API_KEY": KEY}
    assert "mine" in chain_config.config_path().read_text()
    assert "already exists" in await _err(server, "provider.add", _add())  # the retry: the block is there


async def test_the_reload_reads_the_project_a_config_reload_applies(server, monkeypatch, tmp_path):
    project = tmp_path / "elsewhere"
    project.mkdir()
    monkeypatch.setenv("K3CODE_PROJECT_DIR", str(project))  # what `k3code --config-dir` sets; the cwd is tmp_path
    seen = []
    real = config_mod.load_config

    def spy(**kw):
        seen.append(kw.get("project_dir"))
        return real(**kw)

    monkeypatch.setattr(config_mod, "load_config", spy)
    await _ok(server, "provider.add", _add())
    assert seen == [project]
    assert [p.name for p in server.config.providers] == ["first", "mine"]


async def test_a_broken_config_file_is_a_clean_error_without_its_text(server):
    chain_config.config_path().write_text("providers:\n  - name: first\n    SNIPPETMARK: [unclosed\n")
    msg = await _err(server, "provider.add", _add())
    assert "not valid YAML" in msg and "SNIPPETMARK" not in msg
    assert read_env_file() == {}


async def test_a_vendor_preset_keeps_its_own_kind(server, monkeypatch):
    set_env_var("ANTHROPIC_API_KEY", OLD)
    seen = []
    monkeypatch.setattr(probe, "list_models", lambda e, k, timeout=8.0: seen.append(e) or (True, 1.0, ["c"], ""))
    params = _add(preset="anthropic", kind="openai", base_url="https://api.anthropic.com", api_key=None, reuse_key=True)
    await _ok(server, "provider.probe", params)
    assert seen == [{"kind": "anthropic", "base_url": "https://api.anthropic.com"}]
    await _ok(server, "provider.add", params)
    assert yaml.safe_load(chain_config.config_path().read_text())["providers"][-1]["kind"] == "anthropic"


@pytest.mark.parametrize("existing", ["display:\n  tui_start_agents_view: true\n", None])
async def test_the_first_provider_of_a_fresh_install_is_added(server, existing):
    """A fresh home has no providers (or no config file at all): the popup's add form is how the first one gets in."""
    path = chain_config.config_path()
    if existing is None:
        path.unlink()
    else:
        path.write_text(existing)
    server.config.providers = []
    await _ok(server, "provider.add", _add())
    data = yaml.safe_load(path.read_text())
    assert [p["name"] for p in data["providers"]] == ["mine"]
    if existing is not None:
        assert data["display"] == {"tui_start_agents_view": True}  # the rest of the file is kept
    assert [p.name for p in server.config.providers] == ["mine"]


async def test_other_chain_edits_still_need_a_chain_in_the_file(server):
    chain_config.config_path().write_text("display: {}\n")
    with pytest.raises(chain_config.ChainEditError, match="no providers in"):
        chain_config.edit_config("remove", ["first"])
