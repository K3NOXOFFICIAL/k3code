"""/tune: model, reasoning effort and ultracode mode in one command, the tune.get / tune.set RPCs, the persisted
default model, and the session state (ultra_mode) that resume, /fork, /branch and background sessions carry."""

from __future__ import annotations

import types

import pytest
import yaml

import m1cmd_helpers as m1
from k3code import chain_config
from k3code.commands import tune
from k3code.config import ProviderEntry, Settings, load_config
from k3code.gateway.server import HELP_GROUPS
from k3code.providers.effort import takes_effort


def _providers() -> list[ProviderEntry]:
    return [
        ProviderEntry(
            name="a",
            kind="openai",
            base_url="http://a",
            api_key_env="NOPE_A",
            models={"default": "gpt-5.1", "cheap": ["gpt-4o-mini", "o3-mini"], "plain": "llama3"},
            descriptions={"default": "", "cheap": "Fast and cheap"},
        ),
        ProviderEntry(
            name="b",
            kind="anthropic",
            base_url="http://b",
            api_key_env="NOPE_B",
            models={"default": "claude-sonnet-5", "cheap": "claude-haiku-4-5", "deep": "claude-opus-5"},
            descriptions={"default": "Everyday coding", "deep": "Hardest problems"},
        ),
        ProviderEntry(name="c", kind="claude-cli", models={"local": "sonnet"}),
    ]


@pytest.fixture
async def server(tmp_path, monkeypatch):
    srv, _ = m1.make_server(tmp_path, monkeypatch, ["ok"])
    srv.config.providers = _providers()
    assert str(chain_config.config_path()).startswith(str(tmp_path.parent))  # never a real home
    yield srv
    await srv.close()


def _write_config(**extra) -> tuple[object, str]:
    """A user config.yaml in the test's k3code home; returns (path, its exact text)."""
    path = chain_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "default_model": "default",
        "providers": [
            {
                "name": "a",
                "kind": "openai",
                "base_url": "http://a",
                "api_key_env": "NOPE_A",
                "models": {"default": "gpt-5.1", "cheap": "gpt-4o-mini"},
            }
        ],
        **extra,
    }
    text = "# my own comment\n" + yaml.safe_dump(data, sort_keys=False)
    path.write_text(text)
    return path, text


def _backups(path) -> list:
    return sorted(path.parent.glob(path.name + ".bak*"))


def _infos(server, sid) -> list[dict]:
    return [r["payload"] for r in server.event_log if r["type"] == "session.info" and r["session"] == sid]


async def _session(server, tmp_path) -> str:
    return await m1.new_session(server, tmp_path)


# ── grammar ─────────────────────────────────────────────────────────────────────────────────────────────────────


_CFG = Settings(providers=_providers()[:2])
_CFG_KEY_HIGH = Settings(
    providers=[
        ProviderEntry(
            name="t", kind="openai", base_url="http://t", api_key_env="X", models={"default": "m", "high": "h"}
        )
    ]
)


@pytest.mark.parametrize(
    ("arg", "want"),
    [
        ("", {}),
        ("model cheap", {"model": "cheap"}),
        ("cheap", {"model": "cheap"}),
        ("effort high", {"effort": "high"}),
        ("high", {"effort": "high"}),
        ("XHIGH", {"effort": "xhigh"}),
        ("effort default", {"effort": "default"}),
        ("default", {"model": "default"}),  # a model key wins over an effort word of the same name
        ("ultracode", {"ultra_mode": "ultracode"}),
        *[(f"ultracode {w}", {"ultra_mode": "ultracode"}) for w in ("on", "true", "yes", "1", "ON")],
        *[(f"ultracode {w}", {"ultra_mode": "off"}) for w in ("off", "false", "no", "0")],
        ("--reasoning low", {"effort": "low"}),
        ("effort minimal", {"effort": "low"}),  # legacy names, after effort / --reasoning only
        ("--reasoning ultra", {"effort": "max"}),
        ("--reasoning none", {"effort": "default"}),
        ("effort none", {"effort": "default"}),
        ("cheap --global", {"model": "cheap", "scope": "default"}),
        ("cheap --session", {"model": "cheap"}),
        ("cheap --provider b --tui-session --reasoning high", {"model": "cheap", "effort": "high"}),
        ("ultracode off high cheap", {"model": "cheap", "effort": "high", "ultra_mode": "off"}),
        ("effort high high", {"effort": "high"}),  # the same value twice is not a conflict
        ("--session", {}),
    ],
)
def test_the_grammar_takes_every_token_form_in_any_order(arg, want):
    assert tune.parse_tune(_CFG, arg) == tune.TuneRequest(**want)


def test_a_model_key_beats_an_effort_word_but_effort_names_the_level():
    assert tune.parse_tune(_CFG_KEY_HIGH, "high") == tune.TuneRequest(model="high")
    assert tune.parse_tune(_CFG_KEY_HIGH, "effort high") == tune.TuneRequest(effort="high")


@pytest.mark.parametrize(
    ("arg", "message"),
    [
        ("turbo", "Unknown token: turbo"),
        ("--bogus", "Unknown token: --bogus"),
        ("minimal", "Unknown token: minimal"),  # legacy effort names only follow effort / --reasoning
        ("model nope", "Unknown model key: nope (known: cheap, deep, default, local, plain)"),
        ("model", "model needs a value"),
        ("effort", "effort needs a value"),
        ("--reasoning", "--reasoning needs a value"),
        ("--provider", "--provider needs a value"),
        ("effort turbo", "Unknown effort: turbo"),
        ("--reasoning loud", "Unknown effort: loud"),
        ("--global", "--global needs a model"),
        ("high --global", "--global needs a model"),
        ("cheap --global --session", "--global and --session cannot be combined"),
        ("high low", "effort given twice (high, low)"),
        ("cheap default", "model given twice (cheap, default)"),
        ("ultracode maybe", "Unknown token: maybe"),
        ("ultracode on ultracode off", "ultracode given twice (ultracode, off)"),
    ],
)
def test_every_error_names_the_problem(arg, message):
    with pytest.raises(tune.TuneError, match=message.replace("(", r"\(").replace(")", r"\)")):
        tune.parse_tune(Settings(providers=_providers()), arg)


async def test_a_bad_token_replies_with_the_usage_and_changes_nothing(server, tmp_path):
    sid = await _session(server, tmp_path)
    res = await m1.cmd(server, "/tune cheap effort turbo", sid)
    assert res["message"].startswith("Unknown effort: turbo. Usage: /tune ")
    live = server._session_for(sid)
    assert live.stored.model == "default" and live.reasoning_effort is None  # the valid half was not applied
    res = await m1.cmd(server, "/tune loud", sid)
    assert res["message"].startswith("Unknown token: loud. Usage: ")


# ── the command ─────────────────────────────────────────────────────────────────────────────────────────────────


async def test_tune_sets_model_effort_and_ultracode_in_one_go(server, tmp_path):
    sid = await _session(server, tmp_path)
    res = await m1.cmd(server, "/tune cheap effort high ultracode on", sid)
    assert res["message"].splitlines() == [
        "Model key set to: cheap",
        "Reasoning effort set to: high",
        "Ultracode mode set to: on",
    ]
    live = server._session_for(sid)
    assert (live.stored.model, live.reasoning_effort, live.ultra_mode) == ("cheap", "high", "ultracode")
    assert server.config.default_model == "default"  # session scope: the default stays
    stored = server.store.get(sid)
    assert stored.model == "cheap" and stored.meta["reasoning_effort"] == "high"
    assert stored.meta["ultra_mode"] == "ultracode"
    info = _infos(server, sid)[-1]
    assert (info["model_key"], info["reasoning_effort"], info["ultra_mode"]) == ("cheap", "high", "ultracode")

    res = await m1.cmd(server, "/tune --reasoning default ultracode off", sid)
    assert res["message"].splitlines() == ["Reasoning effort set to: default", "Ultracode mode set to: off"]
    assert live.reasoning_effort is None and live.ultra_mode == "off"
    meta = server.store.get(sid).meta
    assert "reasoning_effort" not in meta and "ultra_mode" not in meta


async def test_tune_effort_and_ultracode_need_a_session_and_apply_nothing_without_one(server):
    res = await m1.cmd(server, "/tune cheap effort high")
    assert res["message"] == "No active session."
    assert server.config.default_model == "default"  # the model half was not applied either
    assert (await m1.cmd(server, "/tune ultracode"))["message"] == "No active session."
    assert not chain_config.config_path().exists()


async def test_tune_without_a_session_sets_the_default_model_in_memory_like_model_does(server):
    res = await m1.cmd(server, "/tune cheap")
    assert res["message"] == "Model key set to: cheap" and server.config.default_model == "cheap"
    assert not chain_config.config_path().exists()  # not saved without --global


async def test_bare_tune_reports_the_state(server, tmp_path):
    res = await m1.cmd(server, "/tune")
    assert res["message"] == "Model: default (gpt-5.1) · Effort and ultracode need an active session"
    sid = await _session(server, tmp_path)
    assert (await m1.cmd(server, "/tune", sid))[
        "message"
    ] == "Model: default (gpt-5.1) · Effort: default · Ultracode: off"
    await m1.cmd(server, "/tune deep high ultracode", sid)
    assert (await m1.cmd(server, "/tune", sid))["message"] == (
        "Model: deep (claude-opus-5) · Effort: high · Ultracode: on"
    )


async def test_apply_is_all_or_nothing(server, tmp_path):
    sid = await _session(server, tmp_path)
    live = server._session_for(sid)
    bad = [
        tune.TuneRequest(model="cheap", effort="turbo"),
        tune.TuneRequest(model="cheap", ultra_mode="maybe"),
        tune.TuneRequest(model="nope", effort="high"),
        tune.TuneRequest(model="cheap", scope="everywhere"),
    ]
    for req in bad:
        with pytest.raises(tune.TuneError):
            tune.apply_tune(server, live, req)
        assert (live.stored.model, live.reasoning_effort, live.ultra_mode) == ("default", None, "off")
        assert server.config.default_model == "default"
    with pytest.raises(tune.TuneError, match="No active session"):
        tune.apply_tune(server, None, tune.TuneRequest(model="cheap", effort="high"))
    assert server.config.default_model == "default"


async def test_model_global_saves_the_default_through_the_command_too(server, tmp_path):
    path, text = _write_config()
    sid = await _session(server, tmp_path)
    res = await m1.cmd(server, "/tune cheap --global", sid)
    lines = res["message"].splitlines()
    assert lines[0] == "Model key set to: cheap"
    assert lines[1].startswith("Default model for new sessions: cheap (saved to ") and str(path) in lines[1]
    assert yaml.safe_load(path.read_text())["default_model"] == "cheap"
    (backup,) = _backups(path)
    assert backup.read_text() == text and "backup: " in lines[1]
    assert server.config.default_model == "cheap" and server._session_for(sid).stored.model == "cheap"


# ── the default model is saved to the user config ───────────────────────────────────────────────────────────────


def test_set_default_model_validates_backs_up_and_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    path = chain_config.config_path()
    assert chain_config.set_default_model("cheap") == (True, "")  # no file yet: nothing to back up
    assert yaml.safe_load(path.read_text()) == {"default_model": "cheap"} and not _backups(path)
    assert chain_config.set_default_model("cheap") == (False, "")  # already says so: not rewritten
    assert not _backups(path)
    path, text = _write_config()
    written, backup = chain_config.set_default_model("cheap")
    assert written and (backup, text) == (str(_backups(path)[0]), _backups(path)[0].read_text())
    data = yaml.safe_load(path.read_text())
    assert data["default_model"] == "cheap" and data["providers"][0]["name"] == "a"  # the rest is kept


def test_set_default_model_refuses_what_it_cannot_make_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    path, text = _write_config(max_turns="lots")
    with pytest.raises(chain_config.ChainEditError, match="would be invalid"):
        chain_config.set_default_model("cheap")
    path.write_text("providers: [unclosed")
    with pytest.raises(chain_config.ChainEditError, match="cannot read"):
        chain_config.set_default_model("cheap")
    path.write_text("- a list\n")
    with pytest.raises(chain_config.ChainEditError, match="cannot read"):
        chain_config.set_default_model("cheap")
    assert path.read_text() == "- a list\n" and not _backups(path)  # untouched, and no backup of a failed write


# ── tune.get ────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_tune_get_lists_one_row_per_model_key(server, tmp_path):
    sid = await _session(server, tmp_path)
    await m1.cmd(server, "/tune cheap effort high", sid)
    res = (await m1.rpc(server, "tune.get", {"session_id": sid}))["result"]
    assert res["model"] == "cheap" and res["default_model"] == "default" and res["has_session"] is True
    assert res["efforts"] == ["low", "medium", "high", "xhigh", "max"] and res["effort"] == "high"
    assert res["ultra_modes"] == ["off", "ultracode"] and res["ultra_mode"] == "off"
    assert res["wake_words"] == ["ultracode", "ultraplan", "ultraresearch"] and res["wake_words_enabled"] is True
    rows = {r["key"]: r for r in res["models"]}
    assert [r["key"] for r in res["models"]] == ["default", "cheap", "plain", "deep", "local"]  # default first
    assert rows["default"] == {
        "key": "default",
        "providers": ["a", "b"],
        "resolved": ["gpt-5.1", "claude-sonnet-5"],
        "description": "Everyday coding",  # a's is empty: the first non-empty one wins
        "current": False,
        "default": True,
        "effort": True,
    }
    assert rows["cheap"]["resolved"] == ["gpt-4o-mini", "o3-mini", "claude-haiku-4-5"]
    assert rows["cheap"]["description"] == "Fast and cheap" and rows["cheap"]["current"] is True
    assert rows["cheap"]["effort"] is True  # o3-mini takes one
    assert rows["plain"]["providers"] == ["a"] and rows["plain"]["effort"] is False  # llama takes none
    assert rows["deep"]["providers"] == ["b"] and rows["deep"]["effort"] is True and rows["deep"]["description"]
    assert rows["local"] == {**rows["local"], "providers": ["c"], "resolved": ["sonnet"], "effort": False}
    assert [r["key"] for r in res["models"] if r["current"]] == ["cheap"]
    assert [r["key"] for r in res["models"] if r["default"]] == ["default"]


async def test_tune_get_effort_is_unknown_when_nothing_resolves():
    config = Settings(
        providers=[ProviderEntry(name="d", kind="openai", base_url="http://d", api_key_env="X", models={"blank": ""})],
        default_model="blank",
    )
    (row,) = tune.snapshot(types.SimpleNamespace(config=config), None)["models"]
    assert row["key"] == "blank" and row["resolved"] == [] and row["effort"] is None


async def test_tune_get_without_a_session_and_with_an_unknown_one(server):
    res = (await m1.rpc(server, "tune.get", {}))["result"]
    assert res["has_session"] is False and res["effort"] is None and res["ultra_mode"] == "off"
    assert res["model"] == "default"
    res = (await m1.rpc(server, "tune.get", {"session_id": "nope"}))["result"]
    assert res["has_session"] is False


async def test_tune_get_reports_a_session_model_that_left_the_config(server, tmp_path):
    sid = await _session(server, tmp_path)
    server._session_for(sid).stored.model = "retired"
    res = (await m1.rpc(server, "tune.get", {"session_id": sid}))["result"]
    assert [r["key"] for r in res["models"] if r["current"]] == ["retired"]


@pytest.mark.skipif(
    "wake_words" not in Settings.model_fields, reason="Settings.wake_words is added by the wake word work"
)
async def test_tune_get_reports_the_wake_word_settings(server):
    server.config.wake_words = {"enabled": True, "ultraplan": False}
    res = (await m1.rpc(server, "tune.get", {}))["result"]
    assert res["wake_words"] == ["ultracode", "ultraresearch"] and res["wake_words_enabled"] is True
    server.config.wake_words = {"enabled": False}
    assert (await m1.rpc(server, "tune.get", {}))["result"]["wake_words_enabled"] is False


# ── tune.set ────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_tune_set_applies_and_returns_the_tune_get_shape_plus_changed(server, tmp_path):
    sid = await _session(server, tmp_path)
    res = (
        await m1.rpc(
            server, "tune.set", {"session_id": sid, "model": "cheap", "effort": "xhigh", "ultra_mode": "ultracode"}
        )
    )["result"]
    assert res["ok"] is True and res["changed"] == ["model", "effort", "ultra_mode"]
    assert (res["model"], res["effort"], res["ultra_mode"], res["has_session"]) == ("cheap", "xhigh", "ultracode", True)
    assert set(res) >= set((await m1.rpc(server, "tune.get", {"session_id": sid}))["result"])
    live = server._session_for(sid)
    assert (live.stored.model, live.reasoning_effort, live.ultra_mode) == ("cheap", "xhigh", "ultracode")
    assert _infos(server, sid)[-1]["ultra_mode"] == "ultracode"  # session.info follows

    again = (await m1.rpc(server, "tune.set", {"session_id": sid, "model": "cheap", "effort": "xhigh"}))["result"]
    assert again["changed"] == []
    infos = len(_infos(server, sid))
    await m1.rpc(server, "tune.set", {"session_id": sid, "model": "cheap"})
    assert len(_infos(server, sid)) == infos  # nothing changed: nothing announced

    res = (await m1.rpc(server, "tune.set", {"session_id": sid, "effort": "default", "ultra_mode": "off"}))["result"]
    assert res["changed"] == ["effort", "ultra_mode"] and res["effort"] is None and res["ultra_mode"] == "off"
    # the session is looked up like every other RPC does: without session_id it is the current one
    res = (await m1.rpc(server, "tune.set", {"effort": "low"}))["result"]
    assert res["effort"] == "low" and live.reasoning_effort == "low"


async def test_tune_set_default_scope_saves_the_default_model_with_a_backup(server, tmp_path):
    path, text = _write_config()
    sid = await _session(server, tmp_path)
    res = (
        await m1.rpc(server, "tune.set", {"session_id": sid, "model": "cheap", "effort": "high", "scope": "default"})
    )["result"]
    assert res["changed"] == ["model", "default_model", "effort"]
    assert res["default_model"] == "cheap" and res["model"] == "cheap"
    assert yaml.safe_load(path.read_text())["default_model"] == "cheap"
    (backup,) = _backups(path)
    assert backup.read_text() == text  # the comment is only in the backup
    assert server.config.default_model == "cheap"  # in memory
    assert server._session_for(sid).stored.model == "cheap" and server._session_for(sid).reasoning_effort == "high"
    assert load_config().default_model == "cheap"  # the next start reads it back
    # a new session now starts on it
    new = (await m1.rpc(server, "session.create", {"cwd": str(tmp_path)}))["result"]
    assert new["info"]["model_key"] == "cheap"


async def test_tune_set_session_scope_does_not_persist(server, tmp_path):
    path, text = _write_config()
    sid = await _session(server, tmp_path)
    for params in ({"model": "cheap"}, {"model": "cheap", "scope": "session"}):
        res = (await m1.rpc(server, "tune.set", {"session_id": sid, **params}))["result"]
        assert res["default_model"] == "default"
    assert path.read_text() == text and not _backups(path)
    assert server.config.default_model == "default" and server._session_for(sid).stored.model == "cheap"


async def test_tune_set_default_scope_without_a_model_saves_nothing(server, tmp_path):
    path, text = _write_config()
    sid = await _session(server, tmp_path)
    res = (await m1.rpc(server, "tune.set", {"session_id": sid, "effort": "high", "scope": "default"}))["result"]
    assert res["changed"] == ["effort"] and path.read_text() == text and not _backups(path)


async def test_tune_set_default_scope_writes_nothing_twice(server, tmp_path):
    path, _ = _write_config()
    await m1.rpc(server, "tune.set", {"model": "cheap", "scope": "default"})
    res = (await m1.rpc(server, "tune.set", {"model": "cheap", "scope": "default"}))["result"]
    assert res["changed"] == [] and len(_backups(path)) == 1


async def test_tune_set_without_a_session(server):
    res = (await m1.rpc(server, "tune.set", {"model": "cheap"}))["result"]
    assert res["changed"] == ["default_model"] and res["has_session"] is False
    assert res["model"] == "cheap" and server.config.default_model == "cheap"
    assert not chain_config.config_path().exists()  # not saved without scope default
    err = (await m1.rpc(server, "tune.set", {"effort": "high"}))["error"]
    assert err["code"] == -32602 and "No active session" in err["message"]
    err = (await m1.rpc(server, "tune.set", {"ultra_mode": "ultracode"}))["error"]
    assert "No active session" in err["message"]
    res = (await m1.rpc(server, "tune.set", {"model": "deep", "scope": "default"}))["result"]
    assert yaml.safe_load(chain_config.config_path().read_text()) == {"default_model": "deep"}
    assert res["changed"] == ["default_model"] and server.config.default_model == "deep"


async def test_tune_set_refuses_an_unknown_key_and_applies_nothing(server, tmp_path):
    path, text = _write_config()
    sid = await _session(server, tmp_path)
    err = (
        await m1.rpc(server, "tune.set", {"session_id": sid, "model": "nope", "effort": "high", "scope": "default"})
    )["error"]
    assert err["code"] == -32602 and err["message"].startswith("Unknown model key: nope (known: ")
    live = server._session_for(sid)
    assert (live.stored.model, live.reasoning_effort) == ("default", None)
    assert path.read_text() == text and not _backups(path) and server.config.default_model == "default"


@pytest.mark.parametrize(
    "params",
    [
        {"effort": "turbo"},
        {"effort": "minimal"},  # legacy names are for the typed grammar, not the RPC
        {"ultra_mode": "maybe"},
        {"scope": "everywhere"},
        {"model": 5},
        {"effort": ["high"]},
        {"model": "cheap", "ultra_mode": "on"},
    ],
)
async def test_tune_set_rejects_invalid_values_whole(server, tmp_path, params):
    sid = await _session(server, tmp_path)
    err = (await m1.rpc(server, "tune.set", {"session_id": sid, **params}))["error"]
    assert err["code"] == -32602
    live = server._session_for(sid)
    assert (live.stored.model, live.reasoning_effort, live.ultra_mode) == ("default", None, "off")


async def test_tune_set_treats_null_and_empty_values_as_unset(server, tmp_path):
    sid = await _session(server, tmp_path)
    params = {"session_id": sid, "model": "", "effort": None, "ultra_mode": "", "scope": ""}
    res = (await m1.rpc(server, "tune.set", params))["result"]
    assert res["ok"] is True and res["changed"] == []
    res = (await m1.rpc(server, "tune.set", {**params, "effort": "high"}))["result"]
    assert res["changed"] == ["effort"] and res["model"] == "default"


async def test_a_default_model_that_cannot_be_saved_applies_nothing(server, tmp_path):
    sid = await _session(server, tmp_path)
    path = chain_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()  # a directory where the file should be: the write fails
    err = (
        await m1.rpc(server, "tune.set", {"session_id": sid, "model": "cheap", "effort": "high", "scope": "default"})
    )["error"]
    assert "Could not save the default model" in err["message"]
    live = server._session_for(sid)
    assert (live.stored.model, live.reasoning_effort) == ("default", None) and server.config.default_model == "default"
    res = await m1.cmd(server, "/tune cheap --global", sid)
    assert res["message"].startswith("Could not save the default model") and live.stored.model == "default"
    path.rmdir()
    path.write_text("max_turns: lots\n")  # a config that would not load afterwards: refused, left as it is
    err = (await m1.rpc(server, "tune.set", {"session_id": sid, "model": "cheap", "scope": "default"}))["error"]
    assert "would be invalid" in err["message"] and path.read_text() == "max_turns: lots\n"
    assert server.config.default_model == "default"


# ── session state: resume, fork, branch, background ─────────────────────────────────────────────────────────────


async def test_effort_and_ultracode_survive_a_resume(server, tmp_path):
    sid = await _session(server, tmp_path)
    await m1.cmd(server, "/tune high ultracode on", sid)
    server.live.pop(sid)  # the daemon forgot the live session; the stored row is all that is left
    live = server.live_for(server.store.get(sid))
    assert live.reasoning_effort == "high" and live.ultra_mode == "ultracode"
    assert live.live_info()["ultra_mode"] == "ultracode"
    resumed = (await m1.rpc(server, "session.resume", {"session_id": sid}))["result"]
    assert resumed["info"]["ultra_mode"] == "ultracode" and resumed["info"]["reasoning_effort"] == "high"
    await m1.cmd(server, "/tune ultracode off", sid)
    server.live.pop(sid)
    assert server.live_for(server.store.get(sid)).ultra_mode == "off"


async def test_an_unknown_stored_ultra_mode_is_off(server, tmp_path):
    sid = await _session(server, tmp_path)
    stored = server.store.get(sid)
    stored.meta["ultra_mode"] = "turbo"
    server.live.pop(sid)
    assert server.live_for(stored).ultra_mode == "off"
    stored.meta["ultra_mode"] = "off"  # what a writer that stores the off state looks like
    server.live.pop(sid)
    assert server.live_for(stored).ultra_mode == "off"


async def test_live_info_carries_the_ultra_mode(server, tmp_path):
    res = (await m1.rpc(server, "session.create", {"cwd": str(tmp_path)}))["result"]
    assert res["info"]["ultra_mode"] == "off"


async def test_session_create_takes_an_ultra_mode_and_keeps_it(server, tmp_path):
    res = (await m1.rpc(server, "session.create", {"cwd": str(tmp_path), "ultra_mode": "ultracode"}))["result"]
    assert res["info"]["ultra_mode"] == "ultracode"
    sid = res["session_id"]
    assert server.store.get(sid).meta["ultra_mode"] == "ultracode"
    server.live.pop(sid)
    assert server.live_for(server.store.get(sid)).ultra_mode == "ultracode"
    off = (await m1.rpc(server, "session.create", {"cwd": str(tmp_path), "ultra_mode": "off"}))["result"]
    assert off["info"]["ultra_mode"] == "off" and "ultra_mode" not in server.store.get(off["session_id"]).meta
    n = len(server.store.list())
    err = (await m1.rpc(server, "session.create", {"cwd": str(tmp_path), "ultra_mode": "maybe"}))["error"]
    assert err["code"] == -32602 and "ultra_mode" in err["message"]
    assert len(server.store.list()) == n  # refused before a session row was made


async def test_fork_and_branch_inherit_effort_and_ultracode(server, tmp_path):
    repo = m1.git_repo(tmp_path / "repo")
    sid = await _session(server, repo)
    await m1.cmd(server, "/tune xhigh ultracode on", sid)
    forked = await m1.cmd(server, "/fork", sid)
    live = server.live_for(server.store.get(forked["session_id"]))
    assert live.reasoning_effort == "xhigh" and live.ultra_mode == "ultracode"
    branched = await m1.cmd(server, "/branch k3/tune-test", sid)
    live = server.live_for(server.store.get(branched["session_id"]))
    assert live.reasoning_effort == "xhigh" and live.ultra_mode == "ultracode"
    # the copy is independent: turning it off in the fork leaves the source alone
    await m1.cmd(server, "/tune ultracode off", forked["session_id"])
    assert server._session_for(sid).ultra_mode == "ultracode"


async def test_background_and_ctrl_b_sessions_inherit_effort_and_ultracode(server, tmp_path):
    sid = await _session(server, tmp_path)
    origin = server._session_for(sid)
    await m1.cmd(server, "/tune deep high ultracode on", sid)
    bg = server._fresh_session_like(origin, background=True)  # what /bg <prompt> starts
    assert bg.background and bg.reasoning_effort == "high" and bg.ultra_mode == "ultracode"
    fresh = server.background_current(origin, None)  # Ctrl+B: the client gets a fresh foreground session
    assert not fresh.background and fresh.reasoning_effort == "high" and fresh.ultra_mode == "ultracode"
    for live in (bg, fresh):  # and a resume of those keeps it
        server.live.pop(live.session_id)
        again = server.live_for(server.store.get(live.session_id))
        assert (again.reasoning_effort, again.ultra_mode, again.stored.model) == ("high", "ultracode", "deep")
    off = server._fresh_session_like(server._session_for(sid), background=True)
    await m1.cmd(server, "/tune ultracode off effort default", fresh.session_id)
    plain = server._fresh_session_like(server.live_for(server.store.get(fresh.session_id)))
    assert plain.ultra_mode == "off" and plain.reasoning_effort is None
    assert "ultra_mode" not in plain.stored.meta and "reasoning_effort" not in plain.stored.meta
    assert off.ultra_mode == "ultracode"


# ── config.set model and model.options ──────────────────────────────────────────────────────────────────────────


async def test_config_set_model_honours_reasoning_and_global(server, tmp_path):
    path, text = _write_config()
    sid = await _session(server, tmp_path)
    res = (
        await m1.rpc(
            server,
            "config.set",
            {"key": "model", "session_id": sid, "value": "cheap --provider a --reasoning high --tui-session"},
        )
    )["result"]
    assert res == {"ok": True, "key": "model", "value": "cheap"}
    live = server._session_for(sid)
    assert live.stored.model == "cheap" and live.reasoning_effort == "high"
    assert server.config.default_model == "default" and path.read_text() == text  # session only

    # the old picker's vocabulary
    for level, want in (("minimal", "low"), ("ultra", "max"), ("none", None)):
        await m1.rpc(server, "config.set", {"key": "model", "session_id": sid, "value": f"cheap --reasoning {level}"})
        assert live.reasoning_effort == want

    await m1.rpc(
        server, "config.set", {"key": "model", "session_id": sid, "value": "--reasoning medium default --global"}
    )
    assert yaml.safe_load(path.read_text())["default_model"] == "default"  # unchanged value: not rewritten
    await m1.rpc(server, "config.set", {"key": "model", "session_id": sid, "value": "cheap --provider a --global"})
    assert yaml.safe_load(path.read_text())["default_model"] == "cheap" and server.config.default_model == "cheap"
    assert live.stored.model == "cheap" and len(_backups(path)) == 1


async def test_config_set_model_global_without_a_session_and_bad_values(server, tmp_path):
    path, text = _write_config()
    res = (await m1.rpc(server, "config.set", {"key": "model", "value": "cheap --reasoning high --global"}))["result"]
    assert res["value"] == "cheap"  # --reasoning has no session to go to: ignored, as before
    assert server.config.default_model == "cheap" and yaml.safe_load(path.read_text())["default_model"] == "cheap"
    sid = await _session(server, tmp_path)
    live = server._session_for(sid)
    err = (await m1.rpc(server, "config.set", {"key": "model", "session_id": sid, "value": "deep --reasoning loud"}))[
        "error"
    ]
    assert err["code"] == -32602 and "Unknown effort: loud" in err["message"] and live.stored.model == "cheap"
    err = (await m1.rpc(server, "config.set", {"key": "model", "session_id": sid, "value": "nope --global"}))["error"]
    assert err["message"].startswith("unknown model key: nope")
    assert (
        "model key required"
        in (await m1.rpc(server, "config.set", {"key": "model", "value": "--global"}))["error"]["message"]
    )


async def test_model_options_reports_the_sessions_model(server, tmp_path):
    assert (await m1.rpc(server, "model.options", {}))["result"]["model"] == "default"
    sid = await _session(server, tmp_path)
    await m1.cmd(server, "/tune deep", sid)
    other = await _session(server, tmp_path)  # the current session is now `other`, on the default
    assert (await m1.rpc(server, "model.options", {}))["result"]["model"] == "default"
    res = (await m1.rpc(server, "model.options", {"session_id": sid}))["result"]
    assert res["model"] == "deep" and {p["slug"] for p in res["providers"]} == {"a", "b", "c"}
    assert server.session.session_id == other


# ── /model and /effort keep working ─────────────────────────────────────────────────────────────────────────────


async def test_model_and_effort_aliases_keep_their_messages(server, tmp_path):
    assert (await m1.cmd(server, "/effort high"))["message"] == "No active session."
    assert (await m1.cmd(server, "/effort turbo"))["message"].startswith("Unknown effort: turbo")
    sid = await _session(server, tmp_path)
    assert (await m1.cmd(server, "/model", sid))["message"] == "Current model key: default"
    assert (await m1.cmd(server, "/model cheap", sid))["message"] == "Model key set to: cheap"
    assert (await m1.cmd(server, "/model", sid))["message"] == "Current model key: cheap"
    msg = (await m1.cmd(server, "/model cheapp", sid))["message"]
    assert msg == "Unknown model key: cheapp (known: cheap, deep, default, local, plain)"
    assert (await m1.cmd(server, "/effort", sid))["message"].startswith("Reasoning effort: default. Usage: /effort ")
    assert (await m1.cmd(server, "/effort HIGH", sid))["message"] == "Reasoning effort set to: high"
    assert (await m1.cmd(server, "/effort default", sid))["message"] == "Reasoning effort set to: default"
    assert (await m1.cmd(server, "/effort turbo", sid))["message"].startswith("Unknown effort: turbo. Usage: /effort ")
    assert (await m1.cmd(server, "/effort minimal", sid))["message"].startswith("Unknown effort: minimal. ")
    assert server._session_for(sid).reasoning_effort is None


async def test_model_keeps_the_reason_for_the_learning_record(server, tmp_path):
    seen = []
    server.learning.record = lambda kind, session=None, **kw: seen.append((kind, kw))  # type: ignore[method-assign]
    sid = await _session(server, tmp_path)
    await m1.cmd(server, "/model cheap too slow", sid)
    ((kind, kw),) = seen
    assert kind == "model_switch" and kw["choice"] == "cheap" and kw["subject"] == "default -> cheap"
    assert kw["detail"]["reason"] == "too slow" and kw["detail"]["from"] == "default"
    await m1.cmd(server, "/model cheap", sid)  # same model: not a switch
    await m1.cmd(server, "/tune deep", sid)  # /tune records it as well
    assert [kw["choice"] for _, kw in seen] == ["cheap", "deep"]


async def test_model_takes_the_tune_flags(server, tmp_path):
    path, _ = _write_config()
    sid = await _session(server, tmp_path)
    res = await m1.cmd(server, "/model cheap --reasoning high --global", sid)
    lines = res["message"].splitlines()
    assert lines[0] == "Model key set to: cheap" and lines[-1] == "Reasoning effort set to: high"
    assert lines[1].startswith("Default model for new sessions: cheap")
    assert yaml.safe_load(path.read_text())["default_model"] == "cheap"
    assert server._session_for(sid).reasoning_effort == "high"
    bad = (await m1.cmd(server, "/model deep --because slow", sid))["message"]
    assert (
        bad.startswith("Unknown token: --because. Usage: /model <key>")
        and server._session_for(sid).stored.model == "cheap"
    )


# ── registration, help ──────────────────────────────────────────────────────────────────────────────────────────


async def test_tune_is_a_command_in_the_model_and_settings_help_group(server):
    cat = (await m1.rpc(server, "commands.catalog", {}))["result"]
    names = {p[0] for p in cat["pairs"]}
    assert {"/tune", "/model", "/effort"} <= names
    group = next(c for c in cat["categories"] if c["name"] == "Model and settings")
    assert [p[0] for p in group["pairs"]][:3] == ["/tune", "/model", "/effort"]
    assert any("tune" in names_ for title, names_ in HELP_GROUPS if title == "Model and settings")
    assert "tune" in server.commands.names()


# ── the effort helper and the config field ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "kind", "want"),
    [
        ("claude-sonnet-5", "anthropic", True),
        ("claude-opus-4-5", "anthropic", True),
        ("claude-sonnet-4-6", "anthropic", True),
        ("claude-haiku-4-5", "anthropic", False),
        ("gpt-5.1", "openai", True),
        ("openai/o3-mini", "openai", True),
        ("gpt-4o-mini", "openai", False),
        ("claude-opus-5", "openai", False),  # an OpenAI-style route only ever sends reasoning_effort
        ("sonnet", "claude-cli", False),
        ("", "openai", None),
        ("gpt-5.1", "somethingelse", None),
    ],
)
def test_takes_effort_applies_the_providers_own_test(model, kind, want):
    assert takes_effort(model, kind) is want


def test_descriptions_are_an_optional_provider_field(tmp_path, monkeypatch):
    assert ProviderEntry(name="x", kind="claude-cli").descriptions == {}
    home = tmp_path / "home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "name": "a",
                        "kind": "openai",
                        "base_url": "http://a",
                        "api_key_env": "NOPE_A",
                        "models": {"default": "m"},
                        "descriptions": {"default": "Everyday coding"},
                    }
                ]
            }
        )
    )
    assert load_config().providers[0].descriptions == {"default": "Everyday coding"}
