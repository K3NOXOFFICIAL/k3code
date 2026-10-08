"""K3CODE_<KEY> environment overrides: scalar keys apply, sections are ignored, an empty value counts as unset."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from k3code.config import load_config
from k3code.permissions import InvalidPermissionMode, permission_mode_from_config


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with its own config.yaml. XDG_CONFIG_HOME points at an empty dir, so the real env file is unread."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg_dir = tmp_path / "project" / ".k3code"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("permission_mode: ask\nmax_turns: 7\nautonomy:\n  plan_first: true\n")
    return tmp_path / "project"


def test_autonomy_env_does_not_crash_and_is_ignored(project: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("K3CODE_AUTONOMY", "sekret-value-123")
    with caplog.at_level(logging.WARNING):
        config = load_config(project_dir=project)
    # The config file's mapping is kept: the env value is dropped, not applied.
    assert config.autonomy == {"plan_first": True}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "K3CODE_AUTONOMY" in r.getMessage()]
    assert len(warnings) == 1
    # The value is never echoed (env values can hold secrets).
    assert "sekret-value-123" not in warnings[0].getMessage()


@pytest.mark.parametrize("env_key", ["K3CODE_PROVIDERS", "K3CODE_MCP"])
def test_other_section_env_keys_are_ignored(project: Path, monkeypatch: pytest.MonkeyPatch, env_key: str):
    monkeypatch.setenv(env_key, "a,b")
    config = load_config(project_dir=project)
    assert config.providers == []
    assert config.mcp.servers == {}


def test_permission_mode_env_still_overrides_scalar(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K3CODE_PERMISSION_MODE", "yolo")
    assert load_config(project_dir=project).permission_mode == "yolo"


def test_empty_env_value_counts_as_unset(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K3CODE_MAX_TURNS", "")
    monkeypatch.setenv("K3CODE_AUTONOMY", "")
    config = load_config(project_dir=project)
    assert config.max_turns == 7
    assert config.autonomy == {"plan_first": True}


def test_unknown_permission_mode_env_gets_the_normal_error(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K3CODE_PERMISSION_MODE", "bogus")
    config = load_config(project_dir=project)  # loads; the value is checked where it is used
    with pytest.raises(InvalidPermissionMode, match="permission_mode 'bogus'"):
        permission_mode_from_config("permission_mode", config.permission_mode)


def test_malformed_int_env_is_a_field_validation_error(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K3CODE_MAX_TURNS", "abc")
    with pytest.raises(ValidationError, match="max_turns"):
        load_config(project_dir=project)
