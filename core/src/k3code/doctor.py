"""``/doctor`` and ``k3code doctor``: health checks, each ``ok`` / ``warn`` / ``fail`` with a fix hint.

Secrets are never printed: API keys are only checked for presence.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from k3code import service
from k3code.config import Settings, load_config
from k3code.daemon import k3_home, socket_path
from k3code.reliability.governor import read_psi
from k3code.reliability.journal import ToolJournal
from k3code.reliability.netwatch import NetWatchConfig, _default_provider_probe, default_internet_probe

OK, WARN, FAIL = "ok", "warn", "fail"
#: Hosts that mean "this chain entry goes through OmniRoute".
OMNIROUTE_MARKERS = ("omniroute", ":20128")
MIN_FREE_GB = 5.0


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _is_omniroute(name: str, base_url: str) -> bool:
    text = f"{name} {base_url}".lower()
    return any(m in text for m in OMNIROUTE_MARKERS)


async def _noop_probe() -> tuple[bool, float | None, str]:
    return True, None, "local"


async def _probe_provider(base_url: str, timeout: float = 5.0) -> tuple[bool, float | None, str]:
    start = time.monotonic()
    try:
        status, _body, _url = await _default_provider_probe(base_url, timeout)
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"[:120]
    ms = (time.monotonic() - start) * 1000
    return status < 500, ms, f"HTTP {status}"  # unauthenticated: 401/403 still means "reachable"


async def check_providers(config: Settings, probe: bool = True) -> list[Check]:
    checks: list[Check] = []
    if not config.providers:
        return [Check("providers", FAIL, "no providers configured", f"add providers to {k3_home() / 'config.yaml'}")]
    results: list[tuple[bool, float | None, str]] = []
    if probe:
        results = await asyncio.gather(
            *(_probe_provider(p.base_url) if p.kind != "claude-cli" else _noop_probe() for p in config.providers)
        )
    for i, p in enumerate(config.providers):
        if p.kind == "claude-cli":  # local binary, nothing to reach over HTTP
            path = shutil.which("claude")
            checks.append(
                Check(
                    f"provider:{p.name}",
                    OK if path else FAIL,
                    f"Claude Code CLI at {path}" if path else "Claude Code CLI not found on PATH",
                    "" if path else "install Claude Code and run `claude` once to log in, or remove this entry",
                    {"kind": "claude-cli", "command": path or ""},
                )
            )
            continue
        if not probe:
            checks.append(Check(f"provider:{p.name}", OK, "probe skipped", data={"base_url": p.base_url}))
            continue
        ok, ms, detail = results[i]
        status = OK if ok else FAIL
        if ok and ms is not None and ms > 3000:
            status, detail = WARN, f"{detail}, slow ({ms:.0f} ms)"
        elif ok:
            detail = f"{detail}, {ms:.0f} ms"
        checks.append(
            Check(
                f"provider:{p.name}",
                status,
                detail,
                "" if ok else f"check the network and {p.base_url}",
                {"base_url": p.base_url, "latency_ms": None if ms is None else round(ms, 1)},
            )
        )
    direct = [p.name for p in config.providers if not _is_omniroute(p.name, p.base_url)]
    if direct:
        checks.append(Check("omniroute-bypass", OK, f"direct entries: {', '.join(direct)}"))
    else:
        checks.append(
            Check(
                "omniroute-bypass",
                WARN,
                "every chain entry goes through OmniRoute; if it is down there is no fallback",
                "add a direct provider (e.g. api.anthropic.com) as a later chain entry",
            )
        )
    return checks


def check_keys(config: Settings) -> Check:
    # p.api_key is what load_config resolved from the process environment *or* ~/.config/k3code/env, where the setup
    # wizard stores keys: checking os.environ alone failed every shell run, and so did the update smoke test.
    missing = [
        p.api_key_env
        for p in config.providers
        if p.kind != "claude-cli" and not (p.api_key or os.environ.get(p.api_key_env))
    ]
    if not config.providers:
        return Check("api-keys", WARN, "no providers, nothing to check")
    if missing:
        fake = bool(os.environ.get("K3CODE_FAKE_PROVIDER"))
        return Check(
            "api-keys",
            WARN if fake else FAIL,
            "missing env vars: " + ", ".join(sorted(set(missing))),
            "export them, or put them in ~/.config/k3code/env for the systemd unit",
            {"missing": sorted(set(missing))},
        )
    return Check("api-keys", OK, f"{len(config.providers)} key env var(s) present")


async def check_netwatch_async() -> Check:
    res = await default_internet_probe(NetWatchConfig())
    if res.captive:
        return Check("netwatch", WARN, "captive portal detected", "log in to the network's portal page")
    if res.ok:
        return Check("netwatch", OK, f"online ({res.latency_ms:.0f} ms)" if res.latency_ms else "online")
    return Check("netwatch", FAIL, "offline", "k3code pauses and resumes by itself; check Wi-Fi / cable")


def check_disk(home: Path) -> Check:
    try:
        probe = home if home.exists() else home.parent
        free = shutil.disk_usage(probe).free / 1e9
    except OSError as e:
        return Check("disk", WARN, f"cannot stat: {e}")
    if free < 1:
        return Check(
            "disk",
            FAIL,
            f"{free:.2f} GB free",
            "free disk space; new work is refused below the guard",
            {"free_gb": free},
        )
    if free < MIN_FREE_GB:
        return Check("disk", WARN, f"{free:.1f} GB free", "free some disk space", {"free_gb": free})
    return Check("disk", OK, f"{free:.1f} GB free", data={"free_gb": round(free, 1)})


def check_psi() -> Check:
    io, cpu = read_psi("/proc/pressure/io"), read_psi("/proc/pressure/cpu")
    if not io and not cpu:
        return Check("psi", OK, "PSI unavailable on this kernel (admission control skips it)")
    io10, cpu10 = io.get("some_avg10", 0.0), cpu.get("some_avg10", 0.0)
    data = {"io_some_avg10": io10, "cpu_some_avg10": cpu10}
    if io10 > 40 or cpu10 > 80:
        return Check(
            "psi", WARN, f"io {io10:.1f}%, cpu {cpu10:.1f}% (avg10)", "heavy load; background work will queue", data
        )
    return Check("psi", OK, f"io {io10:.1f}%, cpu {cpu10:.1f}% (avg10)", data=data)


async def check_daemon(sock: Path | None = None) -> Check:
    sock = sock or socket_path()
    if not sock.exists():
        return Check(
            "daemon", WARN, f"not running (no socket at {sock})", "k3code daemon  (or: k3code service install)"
        )
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(sock), limit=1 << 26), 3)
    except (OSError, TimeoutError) as e:
        return Check(
            "daemon", FAIL, f"socket exists but refuses connections: {e}", f"remove {sock} and restart the daemon"
        )
    start = time.monotonic()
    try:
        writer.write(b'{"jsonrpc":"2.0","id":"doctor","method":"setup.status","params":{}}\n')
        await writer.drain()
        while True:
            line = await asyncio.wait_for(reader.readline(), 3)
            if not line:
                raise ConnectionError("closed")
            if json.loads(line).get("id") == "doctor":
                break
        ms = (time.monotonic() - start) * 1000
        return Check("daemon", OK, f"answering on {sock} ({ms:.0f} ms)", data={"socket": str(sock)})
    except (OSError, TimeoutError, ValueError, ConnectionError) as e:
        return Check("daemon", FAIL, f"not answering: {e}", "restart: systemctl --user restart k3code")
    finally:
        writer.close()


def check_systemd() -> Check:
    if not service.is_installed():
        return Check("systemd-unit", OK, "not installed (optional)", "k3code service install")
    state = service.active_state()
    if state == "active":
        return Check("systemd-unit", OK, "k3code.service active")
    return Check(
        "systemd-unit", WARN, f"k3code.service is {state}", "systemctl --user start k3code; journalctl --user -u k3code"
    )


def find_repo_root() -> Path | None:
    from k3code.paths import data_dir

    here = Path(__file__).resolve()
    for candidate in (Path.cwd(), *here.parents, data_dir() / "current"):
        if (candidate / "tui" / "package.json").is_file() or (candidate / "tui" / "dist" / "entry.js").is_file():
            return candidate
    return None


def check_tui() -> Check:
    from k3code.paths import find_node

    node = find_node()
    root = find_repo_root()
    entry = root / "tui" / "dist" / "entry.js" if root else None
    version = ""
    if node:
        r = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
        version = r.stdout.strip()
    if not node:
        return Check("tui", FAIL, "node not found", "install Node.js >= 20")
    major = int(version.lstrip("v").split(".")[0] or 0) if version else 0
    if entry is None or not entry.is_file():
        return Check(
            "tui",
            WARN,
            f"node {version}, TUI build missing",
            "cd tui && npm install && npm run build",
            {"node": version},
        )
    if major < 20:
        return Check("tui", WARN, f"node {version} is old", "upgrade to Node.js >= 20", {"node": version})
    return Check("tui", OK, f"node {version}, tui/dist/entry.js present", data={"node": version})


def check_home(home: Path) -> Check:
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=home, prefix=".doctor-"):
            pass
    except OSError as e:
        return Check("k3code-home", FAIL, f"{home} not writable: {e}", "fix permissions or set K3CODE_HOME")
    return Check("k3code-home", OK, f"{home} writable")


def check_journal(home: Path) -> Check:
    jdir = home / "journal"
    open_sessions: dict[str, int] = {}
    for path in sorted(jdir.glob("*.jsonl")) if jdir.is_dir() else []:
        try:
            j = ToolJournal(home, path.stem)
            try:
                pending = j.resume_plan()
            finally:
                j.close()
        except Exception:  # noqa: BLE001 - a corrupt journal is reported, not fatal
            open_sessions[path.stem] = -1
            continue
        if pending:
            open_sessions[path.stem] = len(pending)
    if open_sessions:
        names = ", ".join(f"{k} ({v if v >= 0 else 'unreadable'})" for k, v in open_sessions.items())
        return Check(
            "journal",
            WARN,
            f"unresolved tool intents in: {names}",
            "k3code --resume --session <id> completes them (side-effect tools are never re-run)",
            {"sessions": open_sessions},
        )
    return Check("journal", OK, "no unresolved intents")


def check_vendor() -> Check:
    root = find_repo_root()
    script = root / "scripts" / "vendor_check.py" if root else None
    if script is None or not script.is_file():
        return Check("vendor", OK, "not a repo checkout; skipped")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    if r.returncode == 0:
        return Check("vendor", OK, "VENDOR.toml consistent")
    last = (r.stdout.strip().splitlines() or ["failed"])[-1]
    return Check("vendor", FAIL, last, "fix VENDOR.toml / the vendored file headers")


def check_sandbox(probe: bool = True) -> Check:
    from k3code.reliability import sandbox

    path = sandbox.bwrap_path()
    if path:
        # bwrap present is not enough: user namespaces may be blocked, and then every sandboxed command fails
        if probe and not sandbox.usable():
            return Check(
                "sandbox",
                WARN,
                "bubblewrap is installed but unusable (user namespaces blocked?); unattended bash runs unsandboxed",
                "allow unprivileged user namespaces for bwrap (try: bwrap --ro-bind / / true)",
            )
        return Check("sandbox", OK, f"bubblewrap at {path}")
    return Check(
        "sandbox",
        WARN,
        "bwrap not found: auto/yolo/background bash runs WITHOUT a sandbox",
        "install bubblewrap (dnf/apt install bubblewrap)",
    )


def check_isolation() -> Check:
    """k3code never reads ~/.hermes/.env (no dotenv loader); flag Hermes env vars that could leak in."""
    leaked = sorted(k for k in os.environ if k.startswith("HERMES_") and k != "HERMES_TUI_GATEWAY_URL")
    if os.environ.get("HERMES_TUI_GATEWAY_URL"):
        return Check(
            "hermes-isolation",
            WARN,
            "HERMES_TUI_GATEWAY_URL is set; it overrides the daemon socket",
            "unset it unless you attach on purpose",
        )
    if leaked:
        return Check(
            "hermes-isolation", WARN, "Hermes env vars present: " + ", ".join(leaked), "unset them in k3code's shell"
        )
    return Check("hermes-isolation", OK, "no Hermes env; k3code reads only its own config and environment")


async def run_checks(config: Settings | None = None, *, probe: bool = True, home: Path | None = None) -> list[Check]:
    home = home or k3_home()
    config = config or load_config(project_dir=Path.cwd())
    checks: list[Check] = []
    checks += await check_providers(config, probe=probe)
    checks.append(check_keys(config))
    checks.append(await check_netwatch_async() if probe else Check("netwatch", OK, "probe skipped"))
    checks += [check_disk(home), check_psi()]
    checks.append(await check_daemon() if probe else Check("daemon", OK, "probe skipped"))
    checks += [
        check_systemd(),
        check_tui(),
        check_home(home),
        check_journal(home),
        check_vendor(),
        # the probe runs bwrap (up to 10 s): off the event loop, since /doctor also runs inside the daemon
        await asyncio.to_thread(check_sandbox, probe),
        check_isolation(),
    ]
    return checks


def summary(checks: list[Check]) -> dict[str, int]:
    return {s: sum(1 for c in checks if c.status == s) for s in (OK, WARN, FAIL)}


def to_json(checks: list[Check]) -> str:
    return json.dumps({"summary": summary(checks), "checks": [asdict(c) for c in checks]}, indent=2, default=str)


def format_report(checks: list[Check]) -> str:
    marks = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = []
    for c in checks:
        lines.append(f"{marks[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            lines.append(f"    fix: {c.fix}")
    s = summary(checks)
    lines.append(f"\n{s[OK]} ok, {s[WARN]} warn, {s[FAIL]} fail")
    return "\n".join(lines)
