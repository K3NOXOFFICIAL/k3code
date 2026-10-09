"""`k3code -p --json` lists the tool calls it made (it printed "tools": [] after a write) and records its model and tool
calls in usage.db (headless runs wrote no rows)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

STEPS = [
    {
        "type": "tool_call",
        "id": "call_1",
        "name": "write",
        "arguments": {"path": "hello.txt", "content": "hi\n"},
        "when": "first",
    },
    {"type": "usage", "prompt_tokens": 12, "completion_tokens": 3},
    {"type": "text", "text": "wrote it", "when": "after_tool"},
]


def test_headless_json_lists_tools_and_usage_db_gets_rows(tmp_path: Path):
    home = tmp_path / "k3home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n- name: fake\n  kind: openai\n  base_url: http://fake\n  api_key_env: K3_TEST_KEY\n"
        "  models: {default: m}\npermission_mode: yolo\n"
    )
    script = tmp_path / "script.json"
    script.write_text(json.dumps(STEPS))
    project = tmp_path / "proj"
    project.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("K3CODE_", "GIT_"))}
    env |= {"K3CODE_HOME": str(home), "K3CODE_FAKE_PROVIDER": str(script), "K3_TEST_KEY": "x", "HOME": str(tmp_path)}
    res = subprocess.run(
        [sys.executable, "-c", "from k3code.cli import cli; cli()", "-p", "write hello", "--json"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert (project / "hello.txt").read_text() == "hi\n"
    assert out["text"] == "wrote it"
    assert [(t["id"], t["name"], t["arguments"]["path"]) for t in out["tools"]] == [("call_1", "write", "hello.txt")]
    assert "hello.txt" in out["tools"][0]["result"]

    db = sqlite3.connect(home / "usage.db")
    rows = db.execute("SELECT kind, session, provider, model, tokens_in, tokens_out, detail FROM events").fetchall()
    db.close()
    calls = [r for r in rows if r[0] == "call"]
    assert len(calls) == 2 and all(r[1] == "headless" and r[2] == "fake" and r[3] == "m" for r in calls)
    assert calls[0][4:6] == (12, 3)
    assert [r[6] for r in rows if r[0] == "tool"] == ["write"]
