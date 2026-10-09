"""Precedence per top-level key: CLI flag > env > project config > user config > defaults.

Nested sections are replaced as a whole, never merged. Every file here lives under pytest's tmp_path;
HOME, XDG_CONFIG_HOME and K3CODE_HOME are redirected so a real ~/.k3code or ~/.config/k3code is never read,
and each project config is recorded as trusted there (an untrusted one is ignored).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from k3code import trust
from k3code.config import load_config

USER_PROVIDER_A = """
providers:
  - name: user-a
    kind: openai
    base_url: https://user-a.example/v1
    api_key_env: K3_PRECEDENCE_KEY
    models: {default: model-a}
"""

PROJECT_PROVIDER_B = """
providers:
  - name: project-b
    kind: openai
    base_url: https://project-b.example/v1
    api_key_env: K3_PRECEDENCE_KEY
    models: {default: model-b}
"""


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    user_home = tmp_path / "k3home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("K3CODE_HOME", str(user_home))
    for key in list(os.environ):
        if key.startswith("K3CODE_") and key != "K3CODE_HOME":
            monkeypatch.delenv(key)
    project = tmp_path / "project"
    project.mkdir()
    return {"user": user_home, "project": project}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def _write_user(dirs: dict[str, Path], text: str) -> None:
    _write(dirs["user"] / "config.yaml", text)


def _write_project(dirs: dict[str, Path], text: str) -> None:
    _write(dirs["project"] / ".k3code" / "config.yaml", text)
    trust.record(dirs["project"], trusted=True)  # an untrusted project config is never applied (k3code.trust)


def test_project_providers_are_ignored_even_when_trusted(dirs: dict[str, Path]) -> None:
    _write_user(dirs, USER_PROVIDER_A)
    _write_project(dirs, PROJECT_PROVIDER_B)
    config = load_config(project_dir=dirs["project"])
    assert [p.name for p in config.providers] == ["user-a"]


def test_user_providers_kept_when_project_does_not_set_them(dirs: dict[str, Path]) -> None:
    _write_user(dirs, USER_PROVIDER_A)
    _write_project(dirs, "permission_mode: auto-edit\n")
    config = load_config(project_dir=dirs["project"])
    assert [p.name for p in config.providers] == ["user-a"]


def test_key_absent_from_project_keeps_user_value(dirs: dict[str, Path]) -> None:
    _write_user(
        dirs,
        USER_PROVIDER_A + "permission_mode: yolo\nautonomy: {plan_first: true}\n",
    )
    _write_project(dirs, PROJECT_PROVIDER_B)
    config = load_config(project_dir=dirs["project"])
    assert config.permission_mode == "yolo"
    assert config.autonomy == {"plan_first": True}


def test_project_scalar_replaces_user_scalar(dirs: dict[str, Path]) -> None:
    _write_user(dirs, "permission_mode: yolo\n")
    _write_project(dirs, "permission_mode: auto-edit\n")
    assert load_config(project_dir=dirs["project"]).permission_mode == "auto-edit"


def test_nested_section_is_replaced_as_a_whole(dirs: dict[str, Path]) -> None:
    _write_user(dirs, "autonomy:\n  plan_first: true\n  fanout: {max_parallel: 3}\n")
    _write_project(dirs, "autonomy:\n  plan_first: false\n")
    config = load_config(project_dir=dirs["project"])
    assert config.autonomy == {"plan_first": False}


def test_env_beats_user_and_project(dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _write_user(dirs, "permission_mode: yolo\n")
    _write_project(dirs, "permission_mode: auto-edit\n")
    monkeypatch.setenv("K3CODE_PERMISSION_MODE", "ask")
    assert load_config(project_dir=dirs["project"]).permission_mode == "ask"


def test_cli_providers_beat_project_and_user(dirs: dict[str, Path]) -> None:
    _write_user(dirs, USER_PROVIDER_A)
    _write_project(dirs, PROJECT_PROVIDER_B)
    cli_provider = {
        "name": "cli-c",
        "kind": "openai",
        "base_url": "https://cli-c.example/v1",
        "api_key_env": "K3_PRECEDENCE_KEY",
        "models": {"default": "model-c"},
    }
    config = load_config(project_dir=dirs["project"], cli_overrides={"providers": [cli_provider]})
    assert [p.name for p in config.providers] == ["cli-c"]


def test_cli_flag_beats_env(dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K3CODE_PERMISSION_MODE", "ask")
    config = load_config(project_dir=dirs["project"], cli_overrides={"permission_mode": "yolo"})
    assert config.permission_mode == "yolo"
