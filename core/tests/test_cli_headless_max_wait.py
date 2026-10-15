"""Issue #108: `k3code -p` with its provider down waited for ever and said nothing; now it says so on stderr, waits
at most --max-wait seconds and exits 75 (EX_TEMPFAIL)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from k3code.cli import HEADLESS_MAX_WAIT, _headless_max_wait


def test_the_flag_wins_then_the_config_then_five_minutes():
    assert _headless_max_wait(None, SimpleNamespace(reliability={})) == HEADLESS_MAX_WAIT == 300.0
    assert _headless_max_wait(None, SimpleNamespace(reliability={"max_wait": 42})) == 42.0
    assert _headless_max_wait(7, SimpleNamespace(reliability={"max_wait": 42})) == 7.0


def test_zero_or_a_null_config_value_waits_for_ever():
    assert _headless_max_wait(0, SimpleNamespace(reliability={"max_wait": 42})) is None
    assert _headless_max_wait(None, SimpleNamespace(reliability={"max_wait": None})) is None


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "k3home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n- name: fake\n  kind: openai\n  base_url: http://fake\n  api_key_env: K3_TEST_KEY\n"
        "  models: {default: m}\npermission_mode: yolo\nreliability: {flags: {netwatch: false}}\n"
    )
    script = tmp_path / "script.json"
    # a connection failure classifies as network: every entry unreachable, so the run parks instead of failing
    script.write_text(json.dumps([{"type": "error", "message": "connection refused"}]))
    project = tmp_path / "proj"
    project.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("K3CODE_")}
    env |= {"K3CODE_HOME": str(home), "K3CODE_FAKE_PROVIDER": str(script), "K3_TEST_KEY": "x", "HOME": str(tmp_path)}
    code = "from k3code.cli import cli; cli()"
    return subprocess.run(
        [sys.executable, "-c", code, "-p", "hi", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_headless_run_with_its_provider_down_says_so_and_gives_up_with_exit_75(tmp_path):
    res = _run(tmp_path, "--max-wait", "1", "--json")
    assert res.returncode == 75, res.stderr
    result = json.loads(res.stdout)
    assert result["error"] == "gave_up" and result["cause"] == "all_providers_unreachable"
    assert result["waited"] == 1.0 and "gave up after 1s" in result["message"]
    assert "[k3code] parked: all 1 provider(s) unreachable; next try in 30s" in res.stderr
    assert "[k3code] gave up: gave up after 1s waiting for a provider (--max-wait" in res.stderr
    assert "k3code: error: gave up after 1s" not in res.stderr  # --json: the result is the report
