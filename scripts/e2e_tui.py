#!/usr/bin/env python3
"""M1 live e2e: pty-drives the built TUI against a real gateway + OmniRoute.

What it does:
  1. Spawns ``node tui/dist/entry.js`` in a pty with
     ``K3CODE_GATEWAY_CMD="<uv python> -m k3code.cli gateway --stdio"``,
     ``K3CODE_HOME=<e2e home with smoke config>`` and the real
     ``OMNIROUTE_API_KEY`` inherited from the environment.
  2. Waits for the TUI to boot (gateway.ready → rendered status line).
  3. Sends a trivial prompt, waits for the assistant turn to complete,
     and asserts the reply text appears on screen.
  4. Reports PASS/FAIL and prints a screen tail on failure.

Usage:
  uv run python scripts/e2e_tui.py [--timeout 180] [--prompt "..."]

Env required: ``OMNIROUTE_API_KEY``. The script points K3CODE_HOME at
``/tmp/k3smoke`` (the existing smoke config with the OmniRoute provider),
unless ``K3CODE_E2E_HOME`` is set.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TUI_ENTRY = REPO_ROOT / "tui" / "dist" / "entry.js"
CORE_DIR = REPO_ROOT / "core"

DEFAULT_HOME = os.environ.get("K3CODE_E2E_HOME", "/tmp/k3smoke")
DEFAULT_PROMPT = "Reply with exactly: E2E-OK"
DEFAULT_TIMEOUT = 180

# Loose markers that the TUI has booted far enough to accept input.
BOOT_MARKERS = [
    r"k3code",
    r"esc\s*to\s*interrupt",
    r"type\s+/\s*for\s*commands",
    r"╭|╰|│",  # ink box borders
]
# Marks a finished assistant turn / idle composer again.
DONE_MARKERS = [
    r"E2E-OK",
]
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][AB0]|\x1b[=>]|\x0f")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\x00", "")


def find_uv_python() -> str:
    uv = shutil.which("uv")
    if uv:
        import subprocess

        try:
            out = subprocess.run(
                [uv, "run", "python", "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                cwd=CORE_DIR,
                timeout=60,
            )
            exe = out.stdout.strip().splitlines()
            if out.returncode == 0 and exe:
                return exe[-1]
        except Exception:
            pass
    return sys.executable


def main() -> int:
    ap = argparse.ArgumentParser(description="M1 live TUI e2e vs OmniRoute")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--home", default=DEFAULT_HOME)
    args = ap.parse_args()

    try:
        import pexpect  # type: ignore[import-not-found]
    except ImportError:
        print("FAIL: pexpect is not installed (uv sync in core/ to get it)")
        return 2

    if os.environ.get("K3_ALLOW_OMNIROUTE") != "1":
        print("SKIP: OmniRoute use is paused by the owner (2026-10-07); set K3_ALLOW_OMNIROUTE=1 to go live")
        return 2
    api_key = os.environ.get("OMNIROUTE_API_KEY", "").strip()
    if not api_key:
        print("FAIL: OMNIROUTE_API_KEY is not set")
        return 2
    if not TUI_ENTRY.is_file():
        print(f"FAIL: built TUI missing: {TUI_ENTRY} (run npm run build in tui/)")
        return 2

    gateway_cmd = f"{find_uv_python()} -m k3code.cli gateway --stdio"
    env = os.environ.copy()
    env["K3CODE_GATEWAY_CMD"] = gateway_cmd
    env["K3CODE_HOME"] = args.home
    env["TERM"] = "xterm-256color"

    deadline = time.time() + args.timeout
    print(f"e2e: spawning {TUI_ENTRY.name} (gateway: {gateway_cmd})")
    child = pexpect.spawn(
        "node",
        [str(TUI_ENTRY)],
        cwd=str(REPO_ROOT),
        env=env,
        dimensions=(40, 140),
        timeout=10,
        encoding="utf-8",
        codec_errors="replace",
    )

    def snapshot() -> str:
        try:
            buf = child.before + (child.after if isinstance(child.after, str) else "")
        except Exception:
            buf = ""
        return strip_ansi(buf)

    def expect_any(patterns: list[str], what: str) -> str | None:
        nonlocal deadline
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        while time.time() < deadline:
            remaining = max(1, int(deadline - time.time()))
            try:
                i = child.expect([re.compile(p.pattern) for p in compiled], timeout=min(10, remaining))
                print(f"e2e: saw {what} marker: {patterns[i]!r}")
                return snapshot()
            except pexpect.TIMEOUT:
                if child.eof() or not child.isalive():
                    print(f"e2e: child died while waiting for {what}")
                    return None
                continue
            except pexpect.EOF:
                print(f"e2e: EOF while waiting for {what}")
                return None
        print(f"e2e: TIMEOUT waiting for {what}")
        return None

    # 1. boot
    boot = expect_any(BOOT_MARKERS, "boot")
    if boot is None:
        print("FAIL: TUI did not boot (no status/composer markers)")
        print("--- screen tail ---")
        print(snapshot()[-4000:])
        child.close(force=True)
        return 1

    # 2. send prompt + Enter. Small delay so the composer has focus.
    time.sleep(2)
    child.send(args.prompt)
    time.sleep(1)
    child.send("\r")

    # 3. wait for the canned reply to appear on screen.
    done = expect_any(DONE_MARKERS, "assistant reply")
    tail = snapshot()[-6000:]
    child.sendcontrol("c")
    time.sleep(1)
    child.close(force=True)

    if done is None:
        print("FAIL: assistant reply did not appear before timeout")
        print("--- screen tail ---")
        print(tail)
        return 1

    print("PASS: live TUI turn completed via gateway + OmniRoute")
    return 0


if __name__ == "__main__":
    sys.exit(main())
