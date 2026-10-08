"""``k3code service install|uninstall|status``: manage the systemd *user* unit for the daemon."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "k3code.service"

#: Canonical unit text; ``install/systemd/k3code.service`` is this with the default ExecStart.
UNIT_TEMPLATE = """\
[Unit]
Description=k3code daemon (keeps coding-agent sessions running 24/7)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=20

[Service]
Type=notify
NotifyAccess=main
ExecStart={exec_start}
Restart=always
RestartSec=5
WatchdogSec=120
Nice=5
IOSchedulingClass=idle
MemoryHigh=2G
Environment=K3CODE_LOG_LEVEL=INFO
# Provider API keys (K3CODE_API_KEY=..., OMNIROUTE_API_KEY=...) go here, chmod 600:
EnvironmentFile=-%h/.config/k3code/env

[Install]
WantedBy=default.target
"""

DEFAULT_EXEC_START = "%h/.local/bin/k3code daemon"


def default_exec_start() -> str:
    """Absolute ExecStart for this install: the ``k3code`` script if on PATH, else this interpreter."""
    exe = shutil.which("k3code")
    if exe:
        return f"{exe} daemon"
    return f"{sys.executable} -m k3code.cli daemon"


def render_unit(exec_start: str | None = None) -> str:
    return UNIT_TEMPLATE.format(exec_start=exec_start or default_exec_start())


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


def install(dry_run: bool = False) -> list[str]:
    """Write the unit, reload, enable --now. Returns the lines describing (or reporting) each step."""
    unit = render_unit()
    path = unit_path()
    steps = [
        f"write {path}",
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {UNIT_NAME}",
    ]
    advice = (
        "To keep it running without a login session, run (needs sudo; k3code never runs it): "
        f"loginctl enable-linger {getpass.getuser()}"
    )
    if dry_run:
        return ["[dry-run] would:", *(f"  - {s}" for s in steps), "[dry-run] unit file:", unit, advice]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit)
    out = [f"wrote {path}"]
    for args in (("daemon-reload",), ("enable", "--now", UNIT_NAME)):
        r = _systemctl(*args)
        out.append(
            f"systemctl --user {' '.join(args)}: " + ("ok" if r.returncode == 0 else f"failed: {r.stderr.strip()}")
        )
    out.append(advice)
    return out


def uninstall(dry_run: bool = False) -> list[str]:
    path = unit_path()
    steps = [f"systemctl --user disable --now {UNIT_NAME}", f"remove {path}", "systemctl --user daemon-reload"]
    if dry_run:
        return ["[dry-run] would:", *(f"  - {s}" for s in steps)]
    out = []
    r = _systemctl("disable", "--now", UNIT_NAME)
    out.append("disable --now: " + ("ok" if r.returncode == 0 else f"failed: {r.stderr.strip()}"))
    if path.exists():
        path.unlink()
        out.append(f"removed {path}")
    _systemctl("daemon-reload")
    return out


def is_installed() -> bool:
    return unit_path().is_file()


def active_state() -> str:
    """``active``/``inactive``/``failed``/... from systemctl, or ``unavailable`` without systemd."""
    if shutil.which("systemctl") is None:
        return "unavailable"
    r = _systemctl("is-active", UNIT_NAME)
    return r.stdout.strip() or r.stderr.strip() or "unknown"


def status() -> list[str]:
    out = [f"unit file: {unit_path()} ({'installed' if is_installed() else 'not installed'})"]
    if is_installed():
        out.append(f"state: {active_state()}")
    return out
