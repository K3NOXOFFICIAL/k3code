#!/usr/bin/env python3
"""Strict TUI e2e: TUI -> k3code gateway -> OmniRoute must create a file on disk.

Answers approval prompts with Enter (default choice) when they appear.
Usage: e2e_file.py <repo_root> <work_dir> <k3code_home>
"""
import os
import re
import sys
import time
from pathlib import Path

import pexpect

repo, work, home = (Path(a) for a in sys.argv[1:4])
work.mkdir(parents=True, exist_ok=True)
(home).mkdir(parents=True, exist_ok=True)
(home / "config.yaml").write_text(
    "providers:\n"
    '  - {name: omniroute, kind: openai, base_url: "http://<omniroute-host>:20128/v1", '
    "api_key_env: OMNIROUTE_API_KEY, models: {default: [auto/coding-manual, auto/best-coding], cheap: auto/coding-cheap}}\n"
)
target = work / os.environ.get("E2E_TARGET", "e2e.txt")
if target.exists():
    target.unlink()

py = os.popen(f"cd {repo/'core'} && uv run python -c 'import sys;print(sys.executable)'").read().strip().splitlines()[-1]
env = dict(os.environ, K3CODE_GATEWAY_CMD=f"{py} -m k3code.cli gateway --stdio", K3CODE_HOME=str(home), TERM="xterm-256color")
child = pexpect.spawn("node", [str(repo / "tui/dist/entry.js")], cwd=str(work), env=env,
                      dimensions=(40, 140), encoding="utf-8", codec_errors="replace", timeout=5)
ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][AB0]|\x1b[=>]")
screen = ""


def pump(sec: float) -> None:
    global screen
    end = time.time() + sec
    while time.time() < end:
        try:
            screen += child.read_nonblocking(65536, timeout=0.5)
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            return


pump(8)
child.send(os.environ.get("E2E_PROMPT", "create a file named e2e.txt in the current directory containing exactly the word works"))
time.sleep(1)
child.send("\r")
approvals = 0
deadline = time.time() + 240
seen_len = 0
while time.time() < deadline:
    pump(3)
    tail = ansi.sub("", screen[seen_len:])
    if re.search(r"allow|approve|approval|permission|run this|\bonce\b", tail, re.I):
        approvals += 1
        seen_len = len(screen)
        child.send("\r")
    if target.exists() and target.read_text().strip() == os.environ.get("E2E_EXPECT", "works"):
        break
ok = target.exists() and target.read_text().strip() == os.environ.get("E2E_EXPECT", "works")
child.sendcontrol("c")
time.sleep(1)
child.close(force=True)
print(f"approval-prompts-answered={approvals}")
print("RESULT", "PASS" if ok else "FAIL", repr(target.read_text() if target.exists() else None))
if not ok:
    print("--- screen tail ---")
    print(ansi.sub("", screen)[-3000:])
sys.exit(0 if ok else 1)
