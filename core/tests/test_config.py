"""Config loading, provider construction, and model-resolution tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from k3code.config import load_config
from k3code.providers import make_providers
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.openai_compat import OpenAICompatProvider


@pytest.fixture
def project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K3_TEST_KEY", "secret-value")
    cfg_dir = tmp_path / ".k3code"
    cfg_dir.mkdir()
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
    cfg_dir = tmp_path / ".k3code"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "providers:\n  - name: x\n    kind: openai\n    base_url: https://x\n"
        "    api_key_env: K3_DOES_NOT_EXIST\n    models: {default: m}\n"
    )
    os.environ.pop("K3_DOES_NOT_EXIST", None)
    config = load_config(project_dir=tmp_path)
    assert config.providers[0].api_key == ""


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
