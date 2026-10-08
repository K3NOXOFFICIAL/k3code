"""A retry after partial output makes the streamed text void (loop.on_text_reset): `k3code -p` and the REPL used to
keep the discarded attempt, so the result read "Hello Hello world"."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path

import pytest

from k3code import cli as cli_mod
from k3code.config import ProviderEntry, Settings
from k3code.permissions import PermissionMode
from test_agent import _FlakyMidStream


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    import k3code.providers as providers_mod
    import k3code.router as router_mod

    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [_FlakyMidStream()])
    monkeypatch.setattr(router_mod, "Router", functools.partial(router_mod.Router, base_delay=0.0, max_delay=0.0))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    return Settings(providers=[prov], default_model="default")


def test_headless_result_holds_only_the_retried_answer(tmp_path, monkeypatch):
    config = _setup(monkeypatch, tmp_path)
    result = asyncio.run(
        cli_mod._run_headless("hi", model=None, permission_mode=PermissionMode.YOLO, config=config, json_output=True)
    )
    assert result is not None and result["text"] == "Hello world", result


def test_repl_marks_the_discarded_partial_reply(tmp_path, monkeypatch, capsys):
    config = _setup(monkeypatch, tmp_path)
    inputs = iter(["hi", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    out = capsys.readouterr().out
    assert "Hello \n" in out and "discarded" in out and out.rstrip().endswith("Hello world"), out
