"""Config loading, provider construction, and model-resolution tests."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest
from pydantic import ValidationError

from k3code.config import load_config
from k3code.providers import make_providers
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.setup.state import set_env_var


@pytest.fixture
def project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K3_TEST_KEY", "secret-value")
    # providers come from the user config only: a project config cannot set them (k3code.config.load_config)
    cfg_dir = Path(os.environ["K3CODE_HOME"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        """
providers:
  - name: test-openai
    kind: openai
    base_url: https://api.test.com/v1
    api_key_env: K3_TEST_KEY
    models:
      default: gpt-test
      cheap: gpt-test-mini
  - name: test-anthropic
    kind: anthropic
    base_url: https://api.anthropic.test
    api_key_env: K3_TEST_KEY
    models:
      default: [claude-test-1, claude-test-2]
default_model: default
"""
    )
    return tmp_path


def test_load_config_expands_api_key(project_config: Path):
    """``api_key_env`` is resolved into ``api_key`` on each ProviderEntry."""
    config = load_config(project_dir=project_config)
    assert len(config.providers) == 2
    for p in config.providers:
        assert p.api_key == "secret-value"
        assert p.api_key_env == "K3_TEST_KEY"


def test_load_config_missing_env_var_yields_empty_key(tmp_path: Path):
    cfg_dir = Path(os.environ["K3CODE_HOME"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "providers:\n  - name: x\n    kind: openai\n    base_url: https://x\n"
        "    api_key_env: K3_DOES_NOT_EXIST\n    models: {default: m}\n"
    )
    os.environ.pop("K3_DOES_NOT_EXIST", None)
    config = load_config(project_dir=tmp_path)
    assert config.providers[0].api_key == ""


def _fake_key() -> str:
    return "sk-fake-" + secrets.token_hex(20)  # built at run time: never a real key


def _leaks(text: str, key: str, width: int = 8) -> bool:
    """True when any ``width``-character piece of the key's random part shows up in ``text``."""
    body = key.removeprefix("sk-fake-")
    return any(body[i : i + width] in text for i in range(len(body) - width + 1))


def _write_user_config(text: str) -> None:
    cfg_dir = Path(os.environ["K3CODE_HOME"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(text)


def _put_key(key: str, source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if source == "environment":
        monkeypatch.setenv("K3_LEAK_KEY", key)
    else:  # the 0600 env file the setup wizard writes (XDG_CONFIG_HOME is a per-test dir)
        monkeypatch.delenv("K3_LEAK_KEY", raising=False)
        set_env_var("K3_LEAK_KEY", key)


#: provider blocks that fail validation as a whole: pydantic echoes the block (input_value=<dict>) in its error
_BROKEN_BLOCKS = {
    "no base_url": "  - name: p\n    kind: openai\n    api_key_env: K3_LEAK_KEY\n",
    "no name": "  - kind: openai\n    base_url: https://x.test\n    api_key_env: K3_LEAK_KEY\n",
    "no kind": "  - name: p\n    base_url: https://x.test\n    api_key_env: K3_LEAK_KEY\n",
    "second block": "  - name: ok\n    kind: openai\n    base_url: https://x.test\n    api_key_env: K3_LEAK_KEY\n"
    "  - name: p\n    kind: anthropic\n    api_key_env: K3_LEAK_KEY\n",
}


@pytest.mark.parametrize("source", ["environment", "env file"])
@pytest.mark.parametrize("block", sorted(_BROKEN_BLOCKS))
def test_invalid_provider_block_error_holds_no_part_of_the_key(
    block: str, source: str, monkeypatch: pytest.MonkeyPatch
):
    """#167: the key is filled in after validation, so a failing provider block cannot echo any of it."""
    key = _fake_key()
    _put_key(key, source, monkeypatch)
    _write_user_config("providers:\n" + _BROKEN_BLOCKS[block])
    with pytest.raises(ValidationError) as exc:
        load_config()
    assert "providers" in str(exc.value)  # the error still says where the problem is
    assert not _leaks(str(exc.value), key)
    assert not _leaks(exc.value.json(), key)
    assert not _leaks(repr(exc.value.errors()), key)


def test_literal_api_key_in_config_is_ignored_and_not_echoed(monkeypatch: pytest.MonkeyPatch):
    """api_key in config.yaml was always replaced by the api_key_env lookup; it does not reach an error either."""
    literal = _fake_key()
    monkeypatch.delenv("K3_LEAK_KEY", raising=False)
    _write_user_config(
        f"providers:\n  - name: p\n    kind: openai\n    api_key_env: K3_LEAK_KEY\n    api_key: {literal}\n"
    )
    with pytest.raises(ValidationError) as exc:
        load_config()
    assert not _leaks(str(exc.value), literal)
    _write_user_config(
        "providers:\n  - name: p\n    kind: openai\n    base_url: https://x.test\n"
        f"    api_key_env: K3_LEAK_KEY\n    api_key: {literal}\n"
    )
    assert load_config().providers[0].api_key == ""


@pytest.mark.parametrize("section", ["providers: null\n", "providers: [just-a-name]\n", "providers: {a: 1}\n"])
def test_malformed_providers_section_is_a_validation_error(section: str):
    """A providers value or entry of the wrong type is a normal config error (it crashed with a TypeError or an
    AttributeError while the key was being filled in)."""
    _write_user_config(section)
    with pytest.raises(ValidationError, match="providers"):
        load_config()


@pytest.mark.parametrize("source", ["environment", "env file"])
def test_loaded_key_is_resolved_but_not_in_repr(source: str, monkeypatch: pytest.MonkeyPatch):
    key = _fake_key()
    _put_key(key, source, monkeypatch)
    _write_user_config(
        "providers:\n  - name: p\n    kind: openai\n    base_url: https://x.test\n    api_key_env: K3_LEAK_KEY\n"
    )
    config = load_config()
    assert config.providers[0].api_key == key
    for text in (repr(config), str(config), repr(config.providers[0]), str(config.providers)):
        assert not _leaks(text, key)


def test_make_providers_builds_correct_types(project_config: Path):
    """make_providers() reads entry.api_key without raising AttributeError."""
    config = load_config(project_dir=project_config)
    providers = make_providers(config.providers)
    assert len(providers) == 2
    assert isinstance(providers[0], OpenAICompatProvider)
    assert isinstance(providers[1], AnthropicProvider)
    assert providers[0].api_key == "secret-value"


def test_resolve_model_specs_picks_default_key(project_config: Path):
    from k3code.cli import _resolve_model_specs

    config = load_config(project_dir=project_config)
    specs = _resolve_model_specs(config)
    assert specs == ["gpt-test", ["claude-test-1", "claude-test-2"]]


def test_resolve_model_specs_falls_back_when_key_missing(project_config: Path):
    from k3code.cli import _resolve_model_specs

    config = load_config(project_dir=project_config)
    config.default_model = "nonexistent-key"
    specs = _resolve_model_specs(config)
    # Falls back to each provider's "default" entry.
    assert specs == ["gpt-test", ["claude-test-1", "claude-test-2"]]


def test_duplicate_provider_names_are_rejected():
    from pydantic import ValidationError

    from k3code.config import Settings

    def block(name: str) -> dict:
        return {"name": name, "kind": "openai", "base_url": "https://x", "api_key_env": "K", "models": {"default": "m"}}

    with pytest.raises(ValidationError, match="duplicate provider name 'Relay'"):
        Settings(providers=[block("relay"), block("Relay ")])
    assert len(Settings(providers=[block("relay"), block("relay-backup")]).providers) == 2


@pytest.mark.parametrize(
    ("text", "auto_continue", "max_turns", "warned"),
    [
        ("auto_continue: auto\n  auto_continue_max_turns: 3", "auto", 3, ""),
        ("auto_continue: true\n  auto_continue_max_turns: '12'", True, 12, ""),
        ("auto_continue: 'off'\n  auto_continue_max_turns: 0", False, 0, ""),
        ("auto_continue_max_turns: eight", None, None, "autonomy.auto_continue_max_turns='eight' is not valid"),
        ("auto_continue_max_turns: -1", None, None, "autonomy.auto_continue_max_turns=-1 is not valid"),
        ("auto_continue_max_turns: 2.5", None, None, "autonomy.auto_continue_max_turns=2.5 is not valid"),
        ("auto_continue: sometimes", None, None, "autonomy.auto_continue='sometimes' is not valid"),
        ("auto_continue: 3", None, None, "autonomy.auto_continue=3 is not valid"),
    ],
)
def test_auto_continue_settings_are_checked_at_load(tmp_path, caplog, text, auto_continue, max_turns, warned):
    """``auto_continue_max_turns: eight`` passed the load and raised (int("eight")) at the first auto-continued prompt;
    ``-1`` silently meant no cap. A bad value is now warned about at load and the default applies."""
    import logging

    from k3code.autonomy import auto_continue_on, autonomy_cfg

    cfg_dir = Path(os.environ["K3CODE_HOME"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(f"autonomy:\n  {text}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="k3code.config"):
        config = load_config(project_dir=tmp_path)
    acfg = autonomy_cfg(config)
    if warned:
        assert warned in caplog.text and "using the default" in caplog.text
    else:
        assert "is not valid" not in caplog.text
    assert acfg["auto_continue"] == (auto_continue if auto_continue is not None else "auto")
    assert acfg["auto_continue_max_turns"] == (max_turns if max_turns is not None else 8)
    assert auto_continue_on(acfg, "auto") == (auto_continue is not False)


@pytest.mark.parametrize(
    ("stale", "fixed"),
    [
        ("claude-sonnet-5", "claude-sonnet-5-5"),
        ("claude-opus-5", "claude-opus-5-5"),
        ("claude-haiku-4-5", "claude-haiku-5-5"),
    ],
)
def test_retired_model_ids_rewrite_to_dated_ids_on_load(stale: str, fixed: str):
    """A config that still names the bare ``claude-sonnet-5``-era ids loads with the
    dated ids the relay catalog carries. Without this the cheap tier sends a dead
    id and goal_judge dies on every tick with "unknown provider for model"."""
    from k3code.config import ProviderEntry

    def block(models: dict, tiers: dict | None = None) -> dict:
        d: dict = {
            "name": "r",
            "kind": "openai",
            "base_url": "https://x",
            "api_key_env": "K",
            "models": models,
        }
        if tiers is not None:
            d["tiers"] = tiers
        return d

    assert ProviderEntry(**block({"default": stale})).models == {"default": fixed}
    assert ProviderEntry(**block({"default": [stale, "gpt-x"]})).models == {"default": [fixed, "gpt-x"]}
    assert ProviderEntry(**block({"default": "m"}, {"cheap": stale})).tiers == {"cheap": fixed}


def test_current_model_ids_pass_through_untouched():
    """Dated ids, gateway aliases and vendor-prefixed ids are not rewritten."""
    from k3code.config import ProviderEntry

    models = {
        "default": "claude-sonnet-5-5",
        "cheap": ["auto/coding-cheap", "claude/claude-sonnet-5"],
    }
    assert (
        ProviderEntry(
            name="r",
            kind="openai",
            base_url="https://x",
            api_key_env="K",
            models=models,
        ).models
        == models
    )
