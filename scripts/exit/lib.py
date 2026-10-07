"""Shared helpers for exit checks. Each check calls emit() once per criterion; rows go to $EXIT_ROWS (JSONL)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATUSES = ("PASS", "FAIL", "PENDING")


def tail(text: str, n: int = 6, width: int = 300) -> str:
    lines = [ln.rstrip()[:width] for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def emit(milestone: str, criterion: str, how: str, status: str, evidence: str, close: str = "") -> None:
    """status in PASS/FAIL/PENDING; close = exact action that would close a PENDING (required for PENDING)."""
    assert status in STATUSES, status
    if status == "PENDING" and not close:
        raise ValueError("PENDING rows need a `close` action")
    row = {"milestone": milestone, "criterion": criterion, "how": how, "status": status,
           "evidence": tail(evidence, 6), "close": close}
    out = os.environ.get("EXIT_ROWS")
    line = json.dumps(row)
    if out:
        with open(out, "a") as f:
            f.write(line + "\n")
    print(f"[{status}] {milestone}: {criterion}", file=sys.stderr)


def run(cmd: str | list[str], cwd: Path | str | None = None, env: dict | None = None, timeout: int = 600) -> tuple[int, str]:
    """Run a command, return (rc, combined output). Never raises on failure/timeout."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, f"TIMEOUT after {timeout}s\n{(e.stdout or b'')[-500:]!r}"


def omniroute_quota() -> tuple[bool, str]:
    """(usable, detail). 429 'daily usage quota' -> (False, reset info). Never prints the key."""
    import urllib.error
    import urllib.request

    key = os.environ.get("OMNIROUTE_API_KEY", "")
    if not key:
        return False, "OMNIROUTE_API_KEY not set"
    req = urllib.request.Request(
        "http://<omniroute-host>:20128/v1/chat/completions",
        data=json.dumps({"model": "auto/coding-cheap", "max_tokens": 5,
                         "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        return False, f"HTTP {e.code} Retry-After={e.headers.get('Retry-After')} {body}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
