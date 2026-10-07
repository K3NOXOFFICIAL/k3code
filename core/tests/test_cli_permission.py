"""`k3code -p` / the REPL honour the configured permission_mode (the --permission flag used to default to "ask" and
silently override it), and a bad configured value is a usage error rather than a traceback."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from k3code import cli as cli_mod


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    modes: list[str] = []

    async def fake_headless(prompt, *, model, permission_mode, config, **kw):  # noqa: ANN001, ANN003
        modes.append(permission_mode.value)
        return {"text": "ok"}

    monkeypatch.setattr(cli_mod, "_run_headless", fake_headless)
    return modes


def invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, *args: str,
           command: object = cli_mod.main, prompt: bool = True) -> Result:
    """Run the CLI with its own home and cwd, so the developer's shell env and project config cannot leak in."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(config)
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.delenv("K3CODE_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("K3CODE_HEADLESS_PERMISSION", raising=False)
    monkeypatch.chdir(tmp_path)
    return CliRunner().invoke(command, ["-p", "hi", *args] if prompt else list(args))  # type: ignore[arg-type]


def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, *args: str,
        command: object = cli_mod.main) -> None:
    res = invoke(tmp_path, monkeypatch, config, *args, command=command)
    assert res.exit_code == 0, res.output


def assert_usage_error(res: Result, message: str) -> None:
    assert res.exit_code == 1, res.output
    assert isinstance(res.exception, SystemExit)  # click's ClickException exit, not an uncaught ValueError
    assert f"Error: {message}" in res.output
    assert "Traceback" not in res.output


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


def test_modes_outside_the_three_named_ones_still_apply(tmp_path, monkeypatch, seen):
    run(tmp_path, monkeypatch, PROV + "permission_mode: auto\n")
    assert seen == ["auto"]


def test_unknown_config_permission_is_a_usage_error_not_a_traceback(tmp_path, monkeypatch, seen):
    res = invoke(tmp_path, monkeypatch, PROV + "permission_mode: bypass\n")
    assert_usage_error(res, "permission_mode 'bypass' is not one of: ask, auto-edit, yolo "
                            "(set in config.yaml or K3CODE_PERMISSION_MODE)")
    assert seen == []


def test_unknown_headless_permission_names_its_own_key(tmp_path, monkeypatch, seen):
    res = invoke(tmp_path, monkeypatch, PROV + "headless_permission: bypass\n")
    assert_usage_error(res, "headless_permission 'bypass' is not one of: ask, auto-edit, yolo "
                            "(set in config.yaml or K3CODE_HEADLESS_PERMISSION)")
    assert seen == []


def test_unknown_config_permission_fails_the_repl_path_too(tmp_path, monkeypatch, seen):
    res = invoke(tmp_path, monkeypatch, PROV + "permission_mode: bypass\n", prompt=False)
    assert_usage_error(res, "permission_mode 'bypass' is not one of: ask, auto-edit, yolo "
                            "(set in config.yaml or K3CODE_PERMISSION_MODE)")
