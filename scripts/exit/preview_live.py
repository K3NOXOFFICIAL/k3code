#!/usr/bin/env python3
"""Time `/preview` on the live fast tier in the real TUI (pexpect + pyte on `node tui/dist/entry.js`).

Prints one JSON line: {"ok": bool, "secs": float, "sketch": bool, "tail": str} or {"skipped": reason}.
Used by m4_autonomy.py (which runs under a uv env without pexpect/pyte). \
Re-execs under `uv run --with pexpect --with pyte`.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import pexpect  # noqa: F401
    import pyte  # noqa: F401
except ImportError:
    os.execvp("uv", ["uv", "run", "--with", "pexpect", "--with", "pyte", "python", __file__, *sys.argv[1:]])

import lib  # noqa: E402
from tui_harness import Tui  # noqa: E402


def main() -> None:
    backend = lib.live_backend()
    if not backend["ok"]:
        print(json.dumps({"skipped": backend["detail"]}))
        return
    root = Path(tempfile.mkdtemp(prefix="preview-live-"))
    home, cwd = root / "home", root / "cwd"
    home.mkdir()
    cwd.mkdir()
    (home / "config.yaml").write_text(lib.live_providers_yaml(backend))
    t = Tui(home, cwd, script=False)
    try:
        t.boot()
        t.pump(1.5)
        t0 = time.monotonic()
        t.line("/preview a CLI todo app")
        # The command's own completion line ("Preview ready", or its timeout/unavailable message) comes after the
        # sketch; matching words like "error" here fired early on unrelated screen text.
        done = t.wait(r"Preview ready|Preview timed out|Preview unavailable", 60, ever=True)
        secs = time.monotonic() - t0
        t.pump(0.5)
        txt = t.ever_text()
    finally:
        t.close()
        shutil.rmtree(root, ignore_errors=True)
    sketch = "Risks" in txt and ("nothing was changed" in txt or "Preview only" in txt)
    print(json.dumps({"ok": bool(done), "secs": round(secs, 1), "sketch": sketch, "tail": txt[-500:],
                      "label": backend["label"]}))


if __name__ == "__main__":
    main()
