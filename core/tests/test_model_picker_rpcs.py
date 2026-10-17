"""The model picker's gateway side (#365): ``model.options`` sends descriptions, per-model capabilities and the key
state; ``model.save_key`` and ``model.disconnect`` store and remove a provider key the way the setup wizard does.

Every key is written to the test's own env file (``$XDG_CONFIG_HOME/k3code/env`` under the conftest's isolated home).
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

import m1cmd_helpers as m1
from k3code.config import ProviderEntry
from k3code.setup.state import env_file_path, read_env_file, set_env_var, unset_env_var

KEY = "-".join(("sk", "test", "0123456789abcdef"))  # built at run time: no key-like literal (gitleaks)


def _providers() -> list[ProviderEntry]:
    return [
        ProviderEntry(
            name="a",
            kind="openai",
            base_url="http://a",
            api_key_env="PICKER_A_KEY",
            api_key="old-a",
            models={"default": "gpt-5.1", "cheap": ["gpt-4o-mini", "o3-mini"], "local": "llama3"},
            descriptions={"default": "", "cheap": "Fast and cheap"},
        ),
        ProviderEntry(
            name="b",
            kind="anthropic",
            base_url="http://b",
            api_key_env="PICKER_B_KEY",
            models={"default": "claude-sonnet-5-5", "cheap": "claude-haiku-5-5"},
            descriptions={"default": "Everyday coding"},
        ),
        ProviderEntry(name="c", kind="claude-cli", models={"local": "sonnet"}),
        ProviderEntry(name="d", kind="openai", base_url="http://localhost:11434/v1", api_key_env="", models={"x": ""}),
    ]


@pytest.fixture
async def server(tmp_path, monkeypatch):
    for name in ("PICKER_A_KEY", "PICKER_B_KEY", "PICKER_SHARED_KEY"):
        monkeypatch.delenv(name, raising=False)
    srv, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    srv.config.providers = _providers()
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


def _rows(res: dict) -> dict[str, dict]:
    return {p["slug"]: p for p in res["providers"]}


# ── model.options ─────────────────────────────────────────────────────────────────────────────────────────────────


async def test_options_send_descriptions_capabilities_and_key_state(server):
    rows = _rows(await _ok(server, "model.options", {}))
    a, b, c, d = rows["a"], rows["b"], rows["c"], rows["d"]
    assert a["descriptions"] == {"cheap": "Fast and cheap"}  # an empty description is left out
    assert b["descriptions"] == {"default": "Everyday coding"}
    # reasoning: the provider's own effort test on each key's first model id
    assert a["capabilities"] == {
        "default": {"reasoning": True, "fast": False},  # gpt-5.1
        "cheap": {"reasoning": False, "fast": False},  # gpt-4o-mini (the first of its list)
        "local": {"reasoning": False, "fast": False},  # llama3
    }
    assert b["capabilities"] == {
        "default": {"reasoning": True, "fast": False},  # claude-sonnet-5-5
        "cheap": {"reasoning": True, "fast": False},  # claude-haiku-5-5
    }
    assert c["capabilities"] == {"local": {"reasoning": True, "fast": False}}  # claude-cli passes --effort
    assert d["capabilities"] == {}  # no model id: unknown, so the picker keeps offering the effort step
    assert (a["auth_type"], a["authenticated"], a["key_env"]) == ("api_key", True, "PICKER_A_KEY")
    assert (b["authenticated"], b["warning"]) == (False, "paste PICKER_B_KEY to activate")
    assert (c["auth_type"], c["authenticated"]) == ("none", True) and "key_env" not in c
    assert (d["auth_type"], d["authenticated"]) == ("none", True)  # a keyless endpoint (Ollama)
    assert "old-a" not in str(rows)  # the key's name only, never its value


# ── model.save_key ────────────────────────────────────────────────────────────────────────────────────────────────


async def test_save_key_stores_the_key_like_setup_and_uses_it(server, tmp_path, caplog):
    sid = await m1.new_session(server, tmp_path)
    caplog.set_level(logging.DEBUG)
    res = await _ok(server, "model.save_key", {"slug": "b", "api_key": f"  {KEY}\n", "session_id": sid})
    row = res["provider"]
    assert (row["slug"], row["authenticated"], row["key_env"]) == ("b", True, "PICKER_B_KEY")
    assert "warning" not in row
    path = env_file_path()
    assert read_env_file() == {"PICKER_B_KEY": KEY}  # set_env_var: the setup wizard's file and format
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    live = next(p for p in server.config.providers if p.name == "b")
    assert live.api_key == KEY  # the next turn's provider is built with it
    assert server.router is None  # rebuilt on the next turn; only the changed block is swapped
    assert KEY not in str(res) and KEY not in caplog.text  # never echoed
    assert "PICKER_B_KEY" not in os.environ  # not exported to the daemon (child_env would drop it anyway)


async def test_save_key_replaces_a_key_from_the_env_file_and_keeps_other_lines(server, tmp_path):
    set_env_var("OTHER", "keep-me")
    set_env_var("PICKER_A_KEY", "old-a")
    await _ok(server, "model.save_key", {"slug": "a", "api_key": KEY})
    assert read_env_file() == {"OTHER": "keep-me", "PICKER_A_KEY": KEY}


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"api_key": KEY}, "missing required param: slug"),
        ({"slug": "b"}, "missing required param: api_key"),
        ({"slug": "b", "api_key": "   "}, "missing required param: api_key"),
        ({"slug": "zz", "api_key": KEY}, "Unknown provider: zz (valid: a, b, c, d)"),
        ({"slug": "c", "api_key": KEY}, "c takes no API key (it uses the Claude Code login)"),
        ({"slug": "d", "api_key": KEY}, "d takes no API key (its endpoint takes none)"),
        ({"slug": "b", "api_key": "sk-1\nPATH=/tmp"}, "does not look like an API key"),
        ({"slug": "b", "api_key": "sk 1"}, "does not look like an API key"),
        ({"slug": "b", "api_key": 'sk-"1'}, "does not look like an API key"),
        ({"slug": "b", "api_key": "sk-1\\"}, "does not look like an API key"),
    ],
)
async def test_save_key_refuses_bad_params_and_writes_nothing(server, params, message):
    assert message in await _err(server, "model.save_key", params)
    assert not env_file_path().exists()
    assert KEY not in await _err(server, "model.save_key", params)  # the error never repeats the key


async def test_the_old_provider_and_key_params_are_refused(server):
    """The stub read ``provider``/``key`` while the TUI sent ``slug``/``api_key`` (the contract's names)."""
    assert "missing required param: slug" in await _err(server, "model.save_key", {"provider": "b", "key": KEY})


async def test_save_key_refuses_a_variable_the_daemon_environment_sets(server, monkeypatch):
    monkeypatch.setenv("PICKER_B_KEY", "exported-elsewhere")
    message = await _err(server, "model.save_key", {"slug": "b", "api_key": KEY})
    assert "PICKER_B_KEY is set in the daemon's environment" in message
    assert not env_file_path().exists()
    assert "exported-elsewhere" not in message


async def test_save_key_updates_a_variable_systemd_loaded_from_the_env_file(server, monkeypatch):
    """Under systemd the unit loads the env file into the daemon's environment: that value is ours to replace."""
    set_env_var("PICKER_A_KEY", "old-a")
    monkeypatch.setenv("PICKER_A_KEY", "old-a")
    await _ok(server, "model.save_key", {"slug": "a", "api_key": KEY})
    assert read_env_file()["PICKER_A_KEY"] == KEY and os.environ["PICKER_A_KEY"] == KEY


async def test_save_key_refuses_a_variable_shared_with_another_endpoint(server):
    for p in server.config.providers[:2]:
        p.api_key_env = "PICKER_SHARED_KEY"
    message = await _err(server, "model.save_key", {"slug": "a", "api_key": KEY})
    assert "PICKER_SHARED_KEY is also the key variable of b (another endpoint)" in message
    assert not env_file_path().exists()


async def test_save_key_needs_an_authenticated_connection(server):
    from k3code.gateway import server as srv

    assert srv._requires_auth("model.save_key", {}) and srv._requires_auth("model.disconnect", {})


# ── model.disconnect ──────────────────────────────────────────────────────────────────────────────────────────────


async def test_disconnect_removes_the_saved_key_and_stops_using_it(server, tmp_path):
    set_env_var("OTHER", "keep-me")
    await _ok(server, "model.save_key", {"slug": "b", "api_key": KEY})
    res = await _ok(server, "model.disconnect", {"slug": "b"})
    assert res["disconnected"] is True and (res["slug"], res["name"]) == ("b", "b")
    assert res["provider"]["authenticated"] is False
    assert read_env_file() == {"OTHER": "keep-me"}  # only that line
    assert next(p for p in server.config.providers if p.name == "b").api_key == ""
    assert server.router is None
    rows = _rows(await _ok(server, "model.options", {}))
    assert rows["b"]["authenticated"] is False and rows["b"]["warning"] == "paste PICKER_B_KEY to activate"
    assert [p.name for p in server.config.providers] == ["a", "b", "c", "d"]  # the block stays


async def test_disconnect_refuses_a_key_from_the_daemon_environment(server, monkeypatch):
    monkeypatch.setenv("PICKER_A_KEY", "exported-elsewhere")
    message = await _err(server, "model.disconnect", {"slug": "a"})
    assert "PICKER_A_KEY is set in the daemon's environment" in message
    assert next(p for p in server.config.providers if p.name == "a").api_key == "old-a"  # still in use, and says so


async def test_disconnect_refuses_claude_cli_and_unknown_providers(server):
    assert "c takes no API key" in await _err(server, "model.disconnect", {"slug": "c"})
    assert "Unknown provider: zz" in await _err(server, "model.disconnect", {"slug": "zz"})


def test_unset_env_var_keeps_other_lines_and_the_mode(tmp_path):
    path = tmp_path / "env"
    assert unset_env_var("A", path) is False  # no file
    set_env_var("A", "1", path)
    set_env_var("B", "2", path)
    path.write_text(path.read_text() + "export A=3\n")
    assert unset_env_var("A", path) is True
    assert read_env_file(path) == {"B": "2"} and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert unset_env_var("A", path) is False
    assert unset_env_var("B", path) is True and path.read_text() == ""


# ── permissions.get ───────────────────────────────────────────────────────────────────────────────────────────────
async def test_permissions_get_returns_mode_modes_and_effective_rules(server):
    res = await _ok(server, "permissions.get", {})
    assert res["mode"] in res["modes"] and "default" in res["modes"]
    assert isinstance(res["rules"], list) and res["rules"]
    first = res["rules"][0]
    assert {"id", "source", "action", "tool", "pattern"} <= set(first)
    assert first["id"].startswith("b")  # builtin rules come first (u/p/s after)


async def test_permissions_get_is_a_read_only_open_method(server):
    from k3code.gateway import server as srv

    # Read-only like model.options/tune.get: no auth gate, session optional.
    assert not srv._requires_auth("permissions.get", {})
    res = await _ok(server, "permissions.get", {"session_id": None})
    assert "mode" in res
