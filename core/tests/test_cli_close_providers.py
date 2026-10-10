"""`k3code -p` and the REPL close their providers before the event loop ends: a claude-cli provider keeps persistent
``claude`` processes, and one left to the garbage collector printed "Event loop is closed" after the answer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from k3code import cli as cli_mod
from k3code.config import ProviderEntry, Settings
from k3code.permissions import PermissionMode
from k3code.providers.fake import FakeProvider


class _Closing(FakeProvider):
    closed = 0

    async def aclose(self) -> None:
        type(self).closed += 1
        await super().aclose()


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    import k3code.providers as providers_mod

    _Closing.closed = 0
    monkeypatch.setattr(
        providers_mod, "make_providers", lambda entries: [_Closing(steps=[{"type": "text", "text": "done"}])]
    )
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    return Settings(providers=[prov], default_model="default")


def test_headless_run_closes_its_providers(tmp_path, monkeypatch):
    config = _setup(monkeypatch, tmp_path)
    result = asyncio.run(
        cli_mod._run_headless("hi", model=None, permission_mode=PermissionMode.YOLO, config=config, json_output=True)
    )
    assert result is not None and result["text"] == "done", result
    assert _Closing.closed == 1


def test_repl_closes_its_providers(tmp_path, monkeypatch):
    config = _setup(monkeypatch, tmp_path)
    inputs = iter(["/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    assert _Closing.closed == 1
