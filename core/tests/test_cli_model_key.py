"""`k3code -m <key>` and the REPL's /model take a model *key*: it must resolve through the config, never reach the
provider as the literal model id ("cheap"); the TUI gets it through the env var load_config reads."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3code import cli as cli_mod
from k3code.config import ProviderEntry, Settings
from k3code.permissions import PermissionMode
from k3code.providers.fake import FakeProvider


def _config() -> Settings:
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE",
                         models={"default": "m-main", "cheap": "m-cheap"})
    return Settings(providers=[prov], default_model="default")


def _fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[FakeProvider]:
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    made: list[FakeProvider] = []

    def make_providers(entries):  # noqa: ANN001
        made.append(FakeProvider(steps=[{"type": "text", "text": "ok"}]))
        return made[-1:]

    import k3code.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_providers", make_providers)
    return made


def test_headless_model_key_resolves_to_the_providers_model(tmp_path, monkeypatch):
    made = _fake(monkeypatch, tmp_path)
    result = asyncio.run(cli_mod._run_headless(
        "hi", model="cheap", permission_mode=PermissionMode.YOLO, config=_config(), json_output=True))
    assert result == {"text": "ok", "tools": []}
    assert [c["model"] for c in made[0].log] == ["m-cheap"]


def test_headless_unknown_model_key_is_an_error(tmp_path, monkeypatch):
    _fake(monkeypatch, tmp_path)
    result = asyncio.run(cli_mod._run_headless(
        "hi", model="nope", permission_mode=PermissionMode.YOLO, config=_config(), json_output=True))
    assert result is not None and result["error"] == "unknown_model"


def test_tui_gets_the_model_key_through_k3code_default_model(tmp_path, monkeypatch):
    entry = tmp_path / "tui" / "dist" / "entry.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("")
    import k3code.paths as paths_mod

    monkeypatch.setattr(paths_mod, "find_node", lambda: "node")
    monkeypatch.setattr(cli_mod, "_find_repo_root", lambda: tmp_path)
    envs: list[dict[str, str]] = []
    monkeypatch.setattr(cli_mod.subprocess, "run",
                        lambda argv, env, check: envs.append(env) or SimpleNamespace(returncode=0))
    with pytest.raises(SystemExit):
        cli_mod._launch_tui(model="cheap")
    assert envs[0]["K3CODE_DEFAULT_MODEL"] == "cheap"
    monkeypatch.setenv("K3CODE_DEFAULT_MODEL", envs[0]["K3CODE_DEFAULT_MODEL"])
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    from k3code.config import load_config

    assert load_config(project_dir=tmp_path).default_model == "cheap"
