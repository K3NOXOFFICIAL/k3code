"""`k3code -p` and the REPL run the same five user-hook events as the gateway (SessionStart, UserPromptSubmit, Stop)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from k3code import cli as cli_mod
from k3code.permissions import PermissionMode
from test_cli_text_reset import _setup


def _config(monkeypatch, tmp_path: Path, hooks_yaml: str):
    config = _setup(monkeypatch, tmp_path)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "config.yaml").write_text("hooks:\n" + hooks_yaml, encoding="utf-8")
    return config


def _headless(config, **kw):
    return asyncio.run(
        cli_mod._run_headless(
            "hi", model=None, permission_mode=PermissionMode.YOLO, config=config, json_output=True, **kw
        )
    )


def test_headless_runs_session_start_prompt_submit_and_stop(tmp_path: Path, monkeypatch) -> None:
    seen = tmp_path / "seen"
    seen.mkdir()
    config = _config(
        monkeypatch,
        tmp_path,
        f"  SessionStart: [{{command: 'cat > {seen}/start.json'}}]\n"
        f"  UserPromptSubmit: [{{command: 'cat > {seen}/submit.json'}}]\n"
        f"  Stop: [{{command: 'cat > {seen}/stop.json'}}]\n",
    )
    project = tmp_path / "elsewhere"
    project.mkdir()
    result = _headless(config, project_dir=project)
    assert result is not None and result["text"] == "Hello world"
    assert json.loads((seen / "submit.json").read_text())["prompt"] == "hi"
    assert json.loads((seen / "start.json").read_text())["source"] == "startup"
    stop = json.loads((seen / "stop.json").read_text())
    assert stop["hook_event_name"] == "Stop" and stop["cwd"] == str(project)


def test_headless_blocked_prompt_never_reaches_the_model(tmp_path: Path, monkeypatch) -> None:
    config = _config(monkeypatch, tmp_path, "  UserPromptSubmit: [{command: \"echo 'has a secret' >&2; exit 2\"}]\n")
    assert _headless(config) == {
        "error": "prompt_blocked",
        "message": "Prompt blocked by a UserPromptSubmit hook: has a secret",
    }


def test_repl_blocks_a_prompt_and_runs_stop(tmp_path: Path, monkeypatch, capsys) -> None:
    stopped = tmp_path / "stopped"
    config = _config(
        monkeypatch,
        tmp_path,
        '  UserPromptSubmit: [{command: "grep -q forbidden && { echo nope >&2; exit 2; }; true"}]\n'
        f"  Stop: [{{command: 'touch {stopped}'}}]\n",
    )
    inputs = iter(["do the forbidden thing", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    assert "Prompt blocked by a UserPromptSubmit hook: nope" in capsys.readouterr().out
    assert not stopped.exists()  # the blocked prompt ran no turn, so no Stop either
    inputs = iter(["fine", "/exit"])
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    assert stopped.exists()
