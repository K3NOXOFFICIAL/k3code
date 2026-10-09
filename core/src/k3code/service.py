"""``k3code service install|uninstall|status``: manage the systemd *user* unit for the daemon."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from k3code import daemon, paths

UNIT_NAME = "k3code.service"
RECOVER_UNIT_NAME = "k3code-recover.service"

#: systemd's start budget is aligned with the daemon's own restart-storm guard: the guard enters safe mode on the
#: start that exceeds RESTART_LIMIT within RESTART_WINDOW_S, and systemd gives up one start later, so safe mode is
#: already in place when systemd stops restarting the daemon.
START_LIMIT_INTERVAL_S = int(daemon.RESTART_WINDOW_S)
START_LIMIT_BURST = daemon.RESTART_LIMIT + 1
#: After systemd gave up (start limit hit), the recovery unit waits this long, then starts the daemon again.
RECOVER_COOLDOWN_S = 1800

#: Canonical unit text; ``install/systemd/k3code.service`` is this with the default ExecStart.
UNIT_TEMPLATE = """\
[Unit]
Description=k3code daemon (keeps coding-agent sessions running 24/7)
# No After/Wants=network-online.target: the user manager has no such target; the daemon's netwatch waits for the
# network itself.
StartLimitIntervalSec={start_interval}
StartLimitBurst={start_burst}
OnFailure={recover_unit}

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
MemoryMax=4G
TasksMax=512
LimitNOFILE=65536
UMask=0077
NoNewPrivileges=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictRealtime=yes
# Deliberately no ProtectHome/ProtectSystem/PrivateTmp/PrivateNetwork: the daemon runs bubblewrap and the user's tools.
Environment=K3CODE_LOG_LEVEL=INFO
{extra_env}# Provider API keys (K3CODE_API_KEY=..., OMNIROUTE_API_KEY=...) go here, chmod 600:
EnvironmentFile=-%h/.config/k3code/env

[Install]
WantedBy=default.target
"""

#: Runs when systemd gave up on the daemon: a cooldown, then reset the failed state and start it again.
RECOVER_TEMPLATE = """\
[Unit]
Description=Recover the k3code daemon after systemd stopped restarting it (start limit hit)

[Service]
Type=oneshot
TimeoutStartSec=infinity
ExecStart=/bin/sh -c 'sleep {cooldown} && systemctl --user reset-failed {unit} && systemctl --user start {unit}'
"""

DEFAULT_EXEC_START = "%h/.local/share/k3code/current/venv/bin/k3code daemon"


def _unit_quote(text: str) -> str:
    """``%`` is a specifier in unit files; a path with spaces needs quotes."""
    text = text.replace("%", "%%")
    return f'"{text}"' if " " in text else text


def default_exec_start() -> str:
    """Absolute ExecStart for this install: ``<data>/current/venv/bin/k3code`` (stable across updates, which only
    repoint ``current``), else the ``k3code`` on PATH, else this interpreter (a dev checkout without an install)."""
    stable = paths.data_dir() / "current" / "venv" / "bin" / "k3code"
    if stable.exists():
        return f"{_unit_quote(str(stable))} daemon"
    exe = shutil.which("k3code")
    if exe:
        return f"{_unit_quote(exe)} daemon"
    return f"{_unit_quote(sys.executable)} -m k3code.cli daemon"


def default_extra_env() -> str:
    """``Environment=`` lines for K3CODE_DATA / K3CODE_HOME, only when they differ from the defaults (the unit would
    otherwise read a different tree than the shell that installed it)."""
    defaults = {
        "K3CODE_DATA": Path.home() / ".local" / "share" / "k3code",
        "K3CODE_HOME": Path.home() / ".k3code",
    }
    actual = {"K3CODE_DATA": paths.data_dir(), "K3CODE_HOME": paths.home()}
    return "".join(
        f'Environment="{name}={str(actual[name]).replace("%", "%%")}"\n'
        for name in defaults
        if actual[name] != defaults[name]
    )


def render_unit(exec_start: str | None = None) -> str:
    """The unit text. An explicit ``exec_start`` is used as given, with no K3CODE_DATA / K3CODE_HOME lines."""
    return UNIT_TEMPLATE.format(
        exec_start=exec_start or default_exec_start(),
        extra_env="" if exec_start else default_extra_env(),
        start_interval=START_LIMIT_INTERVAL_S,
        start_burst=START_LIMIT_BURST,
        recover_unit=RECOVER_UNIT_NAME,
    )


def render_recover_unit() -> str:
    return RECOVER_TEMPLATE.format(cooldown=RECOVER_COOLDOWN_S, unit=UNIT_NAME)


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def recover_unit_path() -> Path:
    return unit_dir() / RECOVER_UNIT_NAME


class ServiceError(RuntimeError):
    """systemd refused, or is not there: the message says which."""


def systemd_available() -> bool:
    return shutil.which("systemctl") is not None


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise ServiceError("systemd user manager not available (no systemctl)") from None


def restart(unit: str = UNIT_NAME) -> None:
    """``reset-failed`` then ``restart``: after a crash loop the start limit refuses a bare restart
    (start-limit-hit), and the daemon would stay down until the recovery unit runs."""
    _systemctl("reset-failed", unit)  # a unit with nothing to reset is not an error
    r = _systemctl("restart", unit)
    if r.returncode != 0:
        raise ServiceError(f"systemctl --user restart {unit} failed: {(r.stderr or r.stdout).strip()[:300]}")


def install(dry_run: bool = False) -> list[str]:
    """Write the unit, reload, enable --now. Returns the lines describing (or reporting) each step."""
    unit = render_unit()
    path = unit_path()
    recover = recover_unit_path()
    steps = [
        f"write {path}",
        f"write {recover}",
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {UNIT_NAME}",
    ]
    advice = (
        "To keep it running without a login session, run (needs sudo; k3code never runs it): "
        f"loginctl enable-linger {getpass.getuser()}"
    )
    if dry_run:
        return ["[dry-run] would:", *(f"  - {s}" for s in steps), "[dry-run] unit file:", unit, advice]
    if not systemd_available():  # checked before anything is written
        raise ServiceError("systemd user manager not available: run `k3code daemon` directly instead")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit)
    recover.write_text(render_recover_unit())
    out = [f"wrote {path}", f"wrote {recover}"]
    for args in (("daemon-reload",), ("enable", "--now", UNIT_NAME)):
        r = _systemctl(*args)
        out.append(
            f"systemctl --user {' '.join(args)}: " + ("ok" if r.returncode == 0 else f"failed: {r.stderr.strip()}")
        )
    out.append(advice)
    return out


def uninstall(dry_run: bool = False) -> list[str]:
    path = unit_path()
    steps = [
        f"systemctl --user disable --now {UNIT_NAME}",
        f"remove {path}",
        f"remove {recover_unit_path()}",
        "systemctl --user daemon-reload",
    ]
    if dry_run:
        return ["[dry-run] would:", *(f"  - {s}" for s in steps)]
    out = []
    r = _systemctl("disable", "--now", UNIT_NAME)
    out.append("disable --now: " + ("ok" if r.returncode == 0 else f"failed: {r.stderr.strip()}"))
    if path.exists():
        path.unlink()
        out.append(f"removed {path}")
    if recover_unit_path().exists():
        recover_unit_path().unlink()
        out.append(f"removed {recover_unit_path()}")
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
