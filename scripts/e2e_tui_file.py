#!/usr/bin/env python3
"""Strict TUI e2e: TUI -> k3code gateway -> OmniRoute must create a file on disk.

Answers approval prompts with Enter (default choice) when they appear.
Usage: e2e_file.py <repo_root> <work_dir> <k3code_home>

Env switches:
  E2E_DAEMON=1  start `k3code daemon` in <k3code_home> and attach the TUI through
                K3CODE_GATEWAY_CMD="… gateway --attach"; after the TUI detaches, a second
                attach (raw JSON-RPC on the socket) must list the session as completed.
  E2E_FAKE=1    use the scripted fake provider (writes the target file via a bash tool call)
                instead of OmniRoute, so the run needs no API quota.
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
FAKE = os.environ.get("E2E_FAKE") == "1"
DAEMON = os.environ.get("E2E_DAEMON") == "1"
if FAKE:
    import json

    (home / "fake.json").write_text(
        json.dumps(
            [
                {"type": "tool_call", "id": "c1", "name": "bash", "when": "first",
                 "arguments": {"command": "printf works > e2e.txt"}},
                {"type": "text", "text": "done: wrote e2e.txt", "when": "after_tool"},
                {"type": "usage", "prompt_tokens": 5, "completion_tokens": 2},
            ]
        )
    )
    (home / "config.yaml").write_text(
        "permission_mode: yolo\nproviders:\n  - {name: fake, kind: openai, base_url: 'http://fake', "
        "api_key_env: PATH, models: {default: m}}\nreliability: {flags: {netwatch: false}}\n"
    )
else:
    (home / "config.yaml").write_text(
        "providers:\n"
        '  - {name: omniroute, kind: openai, base_url: "http://<omniroute-host>:20128/v1", '
        "api_key_env: OMNIROUTE_API_KEY, "
        "models: {default: [auto/coding-manual, auto/best-coding], cheap: auto/coding-cheap}}\n"
    )
target = work / os.environ.get("E2E_TARGET", "e2e.txt")
if target.exists():
    target.unlink()

_probe = f"cd {repo / 'core'} && uv run python -c 'import sys;print(sys.executable)'"
py = os.popen(_probe).read().strip().splitlines()[-1]
gateway_flag = "--attach" if DAEMON else "--stdio"
env = dict(
    os.environ,
    K3CODE_GATEWAY_CMD=f"{py} -m k3code.cli gateway {gateway_flag}",
    K3CODE_HOME=str(home),
    TERM="xterm-256color",
)
if FAKE:
    env["K3CODE_FAKE_PROVIDER"] = str(home / "fake.json")
daemon_proc = None
sock = home / "run" / "gateway.sock"
if DAEMON:
    import subprocess

    env["K3CODE_GATEWAY_SOCKET"] = str(sock)
    daemon_log = (home / "daemon.log").open("w")
    daemon_proc = subprocess.Popen(
        [py, "-m", "k3code.cli", "daemon"], env=env, cwd=str(work), stdout=subprocess.DEVNULL, stderr=daemon_log
    )
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.1)
    print("daemon-socket-up", sock.exists())
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
default_prompt = "create a file named e2e.txt in the current directory containing exactly the word works"
child.send(os.environ.get("E2E_PROMPT", default_prompt))
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
if DAEMON:
    import json
    import socket as _socket

    def rpc(method, **params):
        c = _socket.socket(_socket.AF_UNIX)
        c.settimeout(10)
        c.connect(str(sock))
        f = c.makefile("rwb")
        f.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n").encode())
        f.flush()
        while True:
            frame = json.loads(f.readline())
            if frame.get("id") == 1:
                c.close()
                return frame["result"]

    # TUI detached (ctrl-c above); a second attach must see the session as completed.
    sessions = rpc("session.list")["sessions"]
    print("second-attach session.list:", [(r["id"], r["status"], r["message_count"]) for r in sessions])
    ok = ok and any(r["status"] in ("idle", "completed") and r["message_count"] >= 2 for r in sessions)
    daemon_proc.terminate()
    daemon_proc.wait(timeout=10)
print("RESULT", "PASS" if ok else "FAIL", repr(target.read_text() if target.exists() else None))
if not ok:
    print("--- screen tail ---")
    print(ansi.sub("", screen)[-3000:])
sys.exit(0 if ok else 1)
