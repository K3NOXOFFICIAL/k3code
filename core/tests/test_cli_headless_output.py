"""`k3code -p` output: the answer ends with a newline, and a failed run says why on stderr (not only in a log line)."""

import json
import os
import subprocess
import sys
from pathlib import Path


def _run(tmp_path: Path, steps: list[dict]) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "k3home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n- name: fake\n  kind: openai\n  base_url: http://fake\n  api_key_env: K3_TEST_KEY\n"
        "  models: {default: m}\npermission_mode: yolo\n"
    )
    script = tmp_path / "script.json"
    script.write_text(json.dumps(steps))
    project = tmp_path / "proj"
    project.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("K3CODE_")}
    env |= {"K3CODE_HOME": str(home), "K3CODE_FAKE_PROVIDER": str(script), "K3_TEST_KEY": "x", "HOME": str(tmp_path)}
    code = "from k3code.cli import cli; cli()"
    return subprocess.run(
        [sys.executable, "-c", code, "-p", "hi"], cwd=project, env=env, capture_output=True, text=True, timeout=120
    )


def test_a_headless_answer_ends_with_a_newline(tmp_path):
    res = _run(tmp_path, [{"type": "text", "text": "the answer"}])
    assert res.returncode == 0, res.stderr
    assert res.stdout == "the answer\n"


def test_a_failed_headless_run_names_the_error_on_stderr(tmp_path):
    res = _run(tmp_path, [{"type": "error", "status_code": 401, "message": "Invalid API key"}])
    assert res.returncode == 1
    assert "k3code: error:" in res.stderr and "Invalid API key" in res.stderr
    assert res.stdout == ""
