"""`k3code -p "/project"` runs the slash command, as the TUI would; a prompt that only starts with a path still goes to
the model, and a command that needs an interactive session says so instead of reaching the model."""

import json
import os
import subprocess
import sys
from pathlib import Path

MODEL_TEXT = "the model answered"


def _run(tmp_path: Path, *args: str, providers: bool = True) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "k3home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(
        "permission_mode: yolo\n"
        if not providers
        else "providers:\n- name: fake\n  kind: openai\n  base_url: http://fake\n  api_key_env: K3_TEST_KEY\n"
        "  models: {default: m}\npermission_mode: yolo\n"
    )
    # the fake model answers only a prompt that reached it with /etc/hosts in it, and says so in its text
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"type": "text", "text": MODEL_TEXT, "match": "/etc/hosts"}]))
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0"\n')
    (project / "uv.lock").write_text("version = 1\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("K3CODE_", "GIT_"))}
    env |= {"K3CODE_HOME": str(home), "K3CODE_FAKE_PROVIDER": str(script), "K3_TEST_KEY": "x", "HOME": str(tmp_path)}
    code = "from k3code.cli import cli; cli()"
    return subprocess.run(
        [sys.executable, "-c", code, *args], cwd=project, env=env, capture_output=True, text=True, timeout=120
    )


def test_a_slash_command_prompt_runs_the_command_without_the_model(tmp_path):
    res = _run(tmp_path, "-p", "/project")
    assert res.returncode == 0, res.stderr
    assert "No stacks detected in" in res.stdout and "/project rescan" in res.stdout
    assert MODEL_TEXT not in res.stdout


def test_a_slash_command_needs_no_provider(tmp_path):
    res = _run(tmp_path, "-p", "/project", providers=False)
    assert res.returncode == 0, res.stderr
    assert "No stacks detected in" in res.stdout


def test_a_slash_command_prompt_takes_its_arguments_and_json(tmp_path):
    res = _run(tmp_path, "-p", "/project rescan", "--json")
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["command"] == "/project" and "Re-scanned" in out["text"]
    assert [s["label"] for s in out["data"]["stacks"]] == ["python (uv)"]


def test_a_prompt_that_starts_with_a_path_still_goes_to_the_model(tmp_path):
    res = _run(tmp_path, "-p", "/etc/hosts explain this")
    assert res.returncode == 0, res.stderr
    assert res.stdout == f"{MODEL_TEXT}\n"


def test_a_command_that_needs_a_session_fails_with_one_line(tmp_path):
    res = _run(tmp_path, "-p", "/clear")
    assert res.returncode == 1
    assert res.stderr == "k3code: error: /clear needs an interactive session; run it in the TUI\n"
    assert res.stdout == ""
