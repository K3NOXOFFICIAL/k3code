"""Config writes are atomic (a crash mid-write keeps the old file) and edits of one file do not lose updates."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from k3code import chain_config, confio


def _provider(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "openai",
        "base_url": f"http://{name}/v1",
        "api_key_env": "X_KEY",
        "models": {"default": "m"},
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": [_provider("first")]}))
    return tmp_path


def _no_leftovers(directory: Path) -> None:
    assert sorted(
        p.name for p in directory.iterdir() if not p.name.startswith(("config.yaml.bak", ".config.yaml.lock"))
    ) == ["config.yaml"]


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_write_yaml_crash_keeps_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str) -> None:
    cfg = tmp_path / "config.yaml"
    confio.write_yaml(cfg, {"a": 1})

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, failing, boom)
    with pytest.raises(OSError):
        confio.write_yaml(cfg, {"a": 2}, backup=False)
    monkeypatch.undo()
    assert cfg.read_text() == "a: 1\n"
    _no_leftovers(tmp_path)


def test_edit_config_crash_keeps_the_old_file(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = (home / "config.yaml").read_text()

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("killed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        chain_config.edit_config("add", ["second", "m", "kind=openai", "base_url=http://s/v1", "key_env=S_KEY"], home)
    monkeypatch.undo()
    assert (home / "config.yaml").read_text() == before


def test_write_keeps_the_file_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    cfg.chmod(0o600)
    confio.write_yaml(cfg, {"a": 2}, backup=False)
    assert cfg.stat().st_mode & 0o777 == 0o600


def test_concurrent_edit_config_calls_lose_no_update(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = chain_config.apply_edit

    def slow(*a: Any, **k: Any) -> Any:
        out = real(*a, **k)
        time.sleep(0.05)  # both edits have read the file before either writes, unless the read-modify-write is locked
        return out

    monkeypatch.setattr(chain_config, "apply_edit", slow)
    names = [f"p{i}" for i in range(6)]
    errors: list[BaseException] = []

    def add(name: str) -> None:
        try:
            chain_config.edit_config("add", [name, "m", "kind=openai", f"base_url=http://{name}/v1", "key_env=K"], home)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=add, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    got = [p["name"] for p in yaml.safe_load((home / "config.yaml").read_text())["providers"]]
    assert sorted(got) == sorted(["first", *names])


def test_lock_is_reentrant_in_a_thread(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    with confio.locked(cfg):
        confio.write_yaml(cfg, {"a": 1})  # takes the same lock again; must not deadlock
    assert cfg.read_text() == "a: 1\n"
