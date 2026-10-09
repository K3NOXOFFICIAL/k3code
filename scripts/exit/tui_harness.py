"""Reusable pexpect + pyte harness driving the real k3code TUI (node tui/dist/entry.js) against the gateway.

The gateway runs with the scripted fake provider (K3CODE_FAKE_PROVIDER) so flows need no network.
Assertions are made on the rendered screen (pyte terminal emulation), not on raw escape streams.
Needs pexpect + pyte: m1_tui.py re-execs itself under `uv run --with pexpect --with pyte` when missing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pexpect
import pyte

REPO = Path(__file__).resolve().parents[2]
ROWS, COLS = 50, 160
KEYS = {
    "enter": "\r",
    "esc": "\x1b",
    "shift_tab": "\x1b[Z",
    "tab": "\t",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "ctrl_c": "\x03",
    "ctrl_f": "\x06",
    "ctrl_d": "\x04",
}


def core_python() -> str:
    out = (
        subprocess.run(
            ["uv", "run", "python", "-c", "import sys;print(sys.executable)"],
            cwd=REPO / "core",
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .splitlines()
    )
    return out[-1]


def write_home(
    home: Path,
    script: list[dict[str, Any]] | None,
    config_extra: str = "",
    permission_mode: str = "default",
    cheap: str = "m",
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if script is not None:
        (home / "fake.json").write_text(json.dumps(script))
    (home / "config.yaml").write_text(
        f"permission_mode: {permission_mode}\nproviders:\n  - {{name: fake, kind: openai, base_url: 'http://fake', "
        "api_key_env: PATH, models: {default: m, cheap: " + cheap + "}}\n"
        "reliability: {flags: {netwatch: false}}\n" + config_extra
    )


class Tui:
    def __init__(
        self,
        home: Path,
        cwd: Path,
        *,
        script: bool = True,
        env: dict[str, str] | None = None,
        gateway_args: str = "--stdio",
    ) -> None:
        self.home, self.cwd = home, cwd
        cwd.mkdir(parents=True, exist_ok=True)
        e = dict(
            os.environ,
            K3CODE_GATEWAY_CMD=f"{core_python()} -m k3code.cli gateway {gateway_args}",
            K3CODE_HOME=str(home),
            HOME=str(home / "h"),
            TERM="xterm-256color",
            NO_COLOR="",
        )
        e.pop("NO_COLOR")
        (home / "h").mkdir(parents=True, exist_ok=True)
        # keep uv/cargo caches working with the fake HOME
        e["UV_CACHE_DIR"] = os.environ.get("UV_CACHE_DIR", str(Path(os.environ["HOME"]) / ".cache" / "uv"))
        # script=False means a live session: never inherit a fake-provider setting (m4's test shim sets
        # K3CODE_FAKE_PROVIDER in the process environment, which would silently turn a "live" row into a fake one).
        e.pop("K3CODE_FAKE_PROVIDER", None)
        if script:
            e["K3CODE_FAKE_PROVIDER"] = str(home / "fake.json")
        e.update(env or {})
        self.env = e
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.Stream(self.screen)
        self.ever: list[str] = []  # every distinct non-empty screen line, in first-seen order
        self._seen: set[str] = set()
        self.child = pexpect.spawn(
            "node",
            [str(REPO / "tui/dist/entry.js")],
            cwd=str(cwd),
            env=e,
            dimensions=(ROWS, COLS),
            encoding="utf-8",
            codec_errors="replace",
            timeout=5,
        )

    # --- screen -------------------------------------------------------------------------------
    def pump(self, sec: float = 0.5) -> None:
        end = time.time() + sec
        while True:
            try:
                data = self.child.read_nonblocking(65536, timeout=0.2)
                self.stream.feed(data)
                self._record()
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                return
            if time.time() >= end:
                return

    def _record(self) -> None:
        for ln in self.screen.display:
            s = ln.rstrip()
            if s.strip() and s not in self._seen:
                self._seen.add(s)
                self.ever.append(s)

    def text(self) -> str:
        return "\n".join(ln.rstrip() for ln in self.screen.display).strip()

    def ever_text(self) -> str:
        return "\n".join(self.ever)

    def wait(self, pattern: str, timeout: float = 30, ever: bool = False) -> bool:
        rx = re.compile(pattern, re.I | re.S)
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.4)
            if rx.search(self.ever_text() if ever else self.text()):
                return True
        return False

    def wait_gone(self, pattern: str, timeout: float = 30) -> bool:
        rx = re.compile(pattern, re.I | re.S)
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.4)
            if not rx.search(self.text()):
                return True
        return False

    def mark(self) -> None:
        """Forget history so later ever-searches only see new lines."""
        self.ever, self._seen = [], set()

    # --- input --------------------------------------------------------------------------------
    def key(self, name: str) -> None:
        self.child.send(KEYS[name])
        self.pump(0.3)

    def line(self, text: str, settle: float = 0.6) -> None:
        self.child.send(text)
        self.pump(settle)
        self.child.send("\r")
        self.pump(0.4)

    def boot(self, timeout: float = 40) -> bool:
        return self.wait(r"k3code|type /|❯|>", timeout)

    def close(self) -> None:
        try:
            self.child.sendcontrol("c")
            time.sleep(0.5)
            self.child.sendcontrol("c")
        except OSError:
            pass
        self.child.close(force=True)
