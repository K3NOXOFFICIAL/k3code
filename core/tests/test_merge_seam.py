"""Seams between the bundle import/export, Main's structural redaction, the setup wizard and the config checks:
a partly hidden value is a placeholder, a long model id as a dict key is a name, the local preset is keyless in every
setup path, every retention key is checked, and the /debug dump keeps its ``key`` fields."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import confio, debugdump
from k3code.bundle import (
    Bundle,
    apply_bundle,
    merge_settings,
    read_bundle,
    sensitive_items,
    write_bundle,
)
from k3code.cli import cli
from k3code.config import RETENTION_DEFAULTS, ConfigError, Settings, load_config
from k3code.gateway.sessions import SessionStore
from k3code.paths import user_config_path
from k3code.redact import REDACTED, redact
from k3code.setup.prompter import AnswerPrompter
from k3code.setup.wizard import run_setup
from test_gateway import make_server


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / ".k3code"))
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("K3CODE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("LOCAL_API_KEY", raising=False)
    return tmp_path


# A 32-hex Cloudflare AI Gateway account id and an Azure deployment name: token-shaped URL path segments of 20+
# characters, which redact() hides in part ("https://gateway.ai.cloudflare.com/v1/<redacted>/my-gateway/openai").
CF_ID = "0123456789abcdef0123456789abcdef"
CF_URL = f"https://gateway.ai.cloudflare.com/v1/{CF_ID}/my-gateway/openai"
AZ_NAME = "gpt-4o-2024-08-06-global"
AZ_URL = f"https://res.openai.azure.com/openai/deployments/{AZ_NAME}/v1"
CF_HIDDEN = CF_URL.replace(CF_ID, REDACTED)
MODEL_A = "claude-sonnet-4-5-20250929-thinking-extended-1m"
MODEL_B = "claude-opus-4-5-20251101-thinking-extended-1m"


def _provider(name: str, base_url: str, api_key_env: str = "K") -> dict:
    return {"name": name, "kind": "openai", "base_url": base_url, "api_key_env": api_key_env, "models": {}}


def _export(tmp_path: Path, config: dict) -> Bundle:
    confio.write_yaml(user_config_path(), config)
    out = tmp_path / "x.k3bundle"
    write_bundle(out, store=SessionStore(tmp_path / "s.db"), cwd=tmp_path, session_ids=[])
    return read_bundle(out)


# ── FIX 1: a value that only contains the marker is a placeholder ──


@pytest.mark.parametrize("url, secret", [(CF_URL, CF_ID), (AZ_URL, AZ_NAME)])
@pytest.mark.parametrize("trust", [True, False])
def test_importing_your_own_export_keeps_a_base_url_that_was_hidden_in_part(env, url, secret, trust) -> None:
    config = {"providers": [_provider("gw", url), _provider("other", "https://api.example.com/v1")]}
    bundle = _export(env, config)
    exported = bundle.settings["user"]["providers"][0]["base_url"]
    assert REDACTED in exported and secret not in exported  # the export still hides the segment
    assert sensitive_items(bundle) == []  # nothing in the bundle can change the URL, so the import asks nothing
    rep = apply_bundle(bundle, store=SessionStore(env / "t.db"), cwd=env, sessions=False, trust_bundle=trust)
    assert rep.skipped == []
    assert confio.read_yaml(user_config_path()) == config


def test_a_hidden_url_does_not_hide_a_real_change_in_the_same_entry(env) -> None:
    bundle = _export(env, {"providers": [_provider("gw", CF_URL, "OLD_KEY")]})
    bundle.settings["user"]["providers"][0]["api_key_env"] = "OTHER_KEY"
    (item,) = sensitive_items(bundle)
    assert item.key == "providers" and len(item.diff) == 1
    assert "api_key_env" in item.diff[0] and "base_url" not in item.diff[0]


def test_a_hidden_scalar_setting_is_not_a_change() -> None:
    incoming = {"research": {"searxng_url": "https://searx.example.com/" + REDACTED + "/q"}}
    bundle = Bundle(manifest={"version": 1}, settings={"user": incoming})
    assert sensitive_items(bundle, {"research": {"searxng_url": "https://searx.example.com/real/q"}}) == []


def test_a_partly_hidden_string_is_a_placeholder_in_dicts_and_lists() -> None:
    hidden = "https://h.example.com/" + REDACTED + "/x"
    real = "https://h.example.com/Zq9xLm3Pw8Rt5Vb2Nc7Kd/x"
    # a dict value keeps what is there and is never created
    assert merge_settings({"u": real}, {"u": hidden}) == {"u": real}
    assert merge_settings({}, {"u": hidden}) == {}
    # an aligned list keeps what is there; the arguments around it are the bundle's
    old = {"a": {"args": ["--url", real, "--port", "9"]}}
    assert merge_settings(old, {"a": {"args": ["--url", hidden, "--port", "9000"]}})["a"]["args"] == [
        "--url",
        real,
        "--port",
        "9000",
    ]
    # an unaligned one keeps the marker in its slot, so the arguments after it do not shift (as for an exact marker)
    other = merge_settings({"a": {"args": ["x"]}}, {"a": {"args": ["y", hidden, "z"]}})
    assert other["a"]["args"] == ["y", hidden, "z"]
    # a dict inside an unaligned list: the hidden value is not created
    assert merge_settings({}, {"providers": [{"name": "p", "base_url": hidden}]}) == {"providers": [{"name": "p"}]}


def test_an_exact_marker_keeps_its_contract() -> None:
    assert merge_settings({"u": "real"}, {"u": REDACTED}) == {"u": "real"}
    assert merge_settings({"a": {"args": ["x"]}}, {"a": {"args": ["y", REDACTED, "z"]}})["a"]["args"] == [
        "y",
        REDACTED,
        "z",
    ]


# ── FIX 2: a long model id is a name, not a secret, when it is a dict key ──


def test_long_model_ids_survive_redact_as_dict_keys() -> None:
    cfg = {"models": {MODEL_A: {"context_window": 1000000}, MODEL_B: {"context_window": 500000}}}
    assert redact(cfg) == cfg


def test_a_dict_key_that_is_a_secret_by_shape_is_still_hidden() -> None:
    token = "sk-" + "test0000000000000000"
    assert token not in json.dumps(redact({token: 1, "npx tool --token " + token: "allow"}))


def test_merge_skips_dict_keys_that_hold_the_marker() -> None:
    incoming = {"models": {REDACTED: {"context_window": 1}, "a-" + REDACTED + "-b": {"x": 1}, MODEL_A: {"y": 2}}}
    assert merge_settings({}, incoming) == {"models": {MODEL_A: {"y": 2}}}
    have = {"models": {MODEL_B: {"context_window": 5}}}  # both dicts exist: the merge itself walks the keys
    assert merge_settings(have, incoming) == {"models": {MODEL_B: {"context_window": 5}, MODEL_A: {"y": 2}}}
    # also inside a dict that is only copied (no existing counterpart), e.g. a list of servers
    merged = merge_settings({}, {"l": [{"env": {REDACTED: "v", "ok": "v"}}]})
    assert merged == {"l": [{"env": {"ok": "v"}}]}


def test_two_long_model_ids_make_the_round_trip(env) -> None:
    config = {"models": {MODEL_A: {"context_window": 1000000}, MODEL_B: {"context_window": 500000}}}
    bundle = _export(env, config)
    assert bundle.settings["user"] == config
    user_config_path().unlink()  # a new device
    apply_bundle(bundle, store=SessionStore(env / "t.db"), cwd=env, sessions=False, trust_bundle=True)
    assert confio.read_yaml(user_config_path()) == config
    assert REDACTED not in user_config_path().read_text()


# ── FIX 3: the local preset is keyless in every setup path ──


def _providers(entries: list[dict]) -> list[dict]:
    answers = AnswerPrompter({"providers": {"entries": entries, "test": False}})
    run_setup(answers, only_step="providers", do_probe=False)
    return confio.read_yaml(user_config_path())["providers"]


def test_an_answers_file_local_preset_is_keyless_and_doctor_accepts_it(env) -> None:
    from k3code.doctor import OK, check_keys

    for entry in ({"preset": "local"}, {"preset": "local", "api_key_env": ""}):
        (prov,) = _providers([entry])
        assert (prov["name"], prov["api_key_env"]) == ("local", "")
        assert check_keys(load_config(project_dir=env)).status == OK


def test_the_setup_cli_writes_a_keyless_local_entry(env) -> None:
    answers = env / "a.yaml"
    answers.write_text("providers:\n  entries:\n  - preset: local\n")
    args = ["setup", "--step", "providers", "--non-interactive", "--no-probe", "--answers", str(answers)]
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == 0, r.output
    assert "api_key_env: ''" in user_config_path().read_text()


def test_the_local_preset_keeps_its_variable_when_a_key_comes_with_it_or_is_set(env, monkeypatch) -> None:
    (prov,) = _providers([{"preset": "local", "api_key": "test-local-key"}])
    assert prov["api_key_env"] == "LOCAL_API_KEY"
    monkeypatch.delenv("LOCAL_API_KEY")
    (prov,) = _providers([{"preset": "local"}])
    assert prov["api_key_env"] == "LOCAL_API_KEY"  # now set up in the env file
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    (prov,) = _providers([{"preset": "local", "api_key_env": "LOCAL_API_KEY"}])
    assert prov["api_key_env"] == "LOCAL_API_KEY"  # an explicit name stays


def test_a_remote_preset_still_needs_a_valid_variable_name(env) -> None:
    (prov,) = _providers([{"preset": "openai"}])
    assert prov["api_key_env"] == "OPENAI_API_KEY"
    for bad in ("bad-name", "has space", "1ABC"):
        with pytest.raises(ValueError, match="api_key_env"):
            _providers([{"preset": "openrouter", "api_key_env": bad}])
    with pytest.raises(ValueError, match="api_key_env"):  # a key cannot be stored under no name
        _providers([{"preset": "local", "api_key_env": "", "api_key": "k"}])


def test_an_explicit_empty_name_declares_any_endpoint_keyless(env) -> None:
    (prov,) = _providers([{"preset": "custom", "base_url": "http://127.0.0.1:9/v1", "api_key_env": ""}])
    assert prov["api_key_env"] == ""


class _Bounded(AnswerPrompter):
    """A refused answer is asked again: a regression must fail the test, not loop for ever."""

    def __init__(self, tree: dict) -> None:
        super().__init__(tree)
        self.asked: dict[str, int] = {}

    def text(self, key: str, message: str, default: str = "", secret: bool = False) -> str:
        self.asked[key] = self.asked.get(key, 0) + 1
        assert self.asked[key] < 4, f"{key} asked again and again"
        return super().text(key, message, default, secret)


def test_the_interactive_questions_accept_a_blank_variable_name_for_the_local_preset(env, capsys) -> None:
    """The per-slot answers (providers.preset0, key0, env0) run the loop that asks; a blank answer is the default."""

    def run(preset: str, **answers) -> dict:
        tree = {"providers": {"add0": True, "preset0": preset, "key0": "", "test": False, **answers}}
        run_setup(_Bounded(tree), only_step="providers", do_probe=False)
        return confio.read_yaml(user_config_path())["providers"][0]

    assert run("local")["api_key_env"] == ""  # no variable set up: the default is blank
    assert "NOTE" not in capsys.readouterr().out  # nothing to export for a keyless entry
    assert run("local", env0="")["api_key_env"] == ""
    assert run("local", env0="MY_LOCAL_KEY")["api_key_env"] == "MY_LOCAL_KEY"
    assert run("openrouter")["api_key_env"] == "OPENROUTER_API_KEY"


# ── FIX 4: every retention key is checked ──


@pytest.mark.parametrize("key", sorted(RETENTION_DEFAULTS))
@pytest.mark.parametrize("value", ["abc", -5, 0, True])
def test_every_retention_key_is_validated(key, value) -> None:
    with pytest.raises(ConfigError, match=f"retention.{key}"):
        confio.validate({"retention": {key: value}})


def test_valid_retention_values_pass() -> None:
    confio.validate({"retention": dict.fromkeys(RETENTION_DEFAULTS, 7)})
    confio.validate({"retention": {}})


# ── FIX 5: the /debug dump keeps the ordinary `key` fields of its events ──


def _files(path: Path) -> dict[str, str]:
    with tarfile.open(path) as tar:
        return {n: tar.extractfile(n).read().decode() for n in tar.getnames()}


async def test_event_fields_named_key_survive_the_dump_but_a_secret_value_does_not(tmp_path) -> None:
    server = make_server(config=Settings())
    server.emit("notification.clear", {"key": "k3.halt", "model_key": "default"})
    server.emit("x.secret", {"key": "sk-" + "live0000000000000000", "model_key": "ghp_" + "a1B2c3D4e5" * 4})
    server.emit("x.named", {"token": "plain-value", "nested": {"api_key": "plain-value"}})
    files = _files(await debugdump.write_bundle(server, 50, home=tmp_path, probe=False))
    payloads = [e["payload"] for e in json.loads(files["events.json"])]
    assert payloads[0] == {"key": "k3.halt", "model_key": "default"}
    assert payloads[1] == {"key": REDACTED, "model_key": REDACTED}  # the names are exempt, not their values
    assert payloads[2] == {"token": REDACTED, "nested": {"api_key": REDACTED}}  # other secret names still hide


async def test_the_key_exemption_is_for_events_only(tmp_path, monkeypatch) -> None:
    calls: list[frozenset] = []
    real = debugdump.scrub_json

    def spy(body, secrets, **kw):
        calls.append(kw.get("keep", frozenset()))
        return real(body, secrets, **kw)

    monkeypatch.setattr(debugdump, "scrub_json", spy)
    await debugdump.write_bundle(make_server(config=Settings()), 5, home=tmp_path, probe=False)
    # the parts in order: events.json, config.json, versions.json, doctor.json
    assert calls == [frozenset({"key", "model_key"}), frozenset(), frozenset(), frozenset()]


def test_redact_keep_exempts_a_name_but_not_a_secret_parent_or_value() -> None:
    keep = frozenset({"key"})
    assert redact({"key": "k3.halt"}) == {"key": REDACTED}
    assert redact([{"key": "k3.halt"}], keep=keep) == [{"key": "k3.halt"}]
    assert redact({"headers": {"key": "k3.halt"}}, keep=keep) == {"headers": {"key": REDACTED}}
    assert redact({"key": "sk-" + "live0000000000000000"}, keep=keep) == {"key": REDACTED}
    assert debugdump.scrub_json('{"key": "k3.halt"}', [], keep=keep) == '{\n  "key": "k3.halt"\n}'
    assert json.loads(debugdump.scrub_json('{"key": "k3.halt"}', [])) == {"key": REDACTED}


def test_file_stamps_are_not_carried_in_by_an_imported_bundle() -> None:
    from k3code.bundle import _UNTRUSTED_META

    assert "file_stamps" in _UNTRUSTED_META
