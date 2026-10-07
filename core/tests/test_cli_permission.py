"""`k3code -p` / the REPL honour the configured permission_mode (the --permission flag used to default to "ask" and
silently override it)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import cli as cli_mod


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    modes: list[str] = []

    async def fake_headless(prompt, *, model, permission_mode, config, **kw):  # noqa: ANN001, ANN003
        modes.append(permission_mode.value)
        return {"text": "ok"}

    monkeypatch.setattr(cli_mod, "_run_headless", fake_headless)
    return modes


def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, *args: str) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(config)
    monkeypatch.setenv("K3CODE_HOME", str(home))
    res = CliRunner().invoke(cli_mod.main, ["-p", "hi", *args])
    assert res.exit_code == 0, res.output


PROV = "providers:\n  - {name: p, kind: claude-cli, models: {default: m}}\n"


def test_config_permission_mode_applies_to_headless(tmp_path, monkeypatch, seen):
    run(tmp_path, monkeypatch, PROV + "permission_mode: yolo\n")
    assert seen == ["yolo"]


def test_default_is_ask_without_config(tmp_path, monkeypatch, seen):
    run(tmp_path, monkeypatch, PROV)
    assert seen == ["default"]  # "ask" is the alias of PermissionMode.DEFAULT


def test_headless_permission_beats_config_and_flag_beats_both(tmp_path, monkeypatch, seen):
    cfg = PROV + "permission_mode: ask\nheadless_permission: auto-edit\n"
    run(tmp_path, monkeypatch, cfg)
    run(tmp_path, monkeypatch, cfg, "--permission", "yolo")
    assert seen == ["accept-edits", "yolo"]
