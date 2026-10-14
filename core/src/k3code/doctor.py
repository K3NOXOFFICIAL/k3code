"""``/doctor`` and ``k3code doctor``: health checks, each ``ok`` / ``warn`` / ``fail`` with a fix hint.

Secrets are never printed: API keys are only checked for presence.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from k3code import service
from k3code.config import Settings, default_project_dir, load_config
from k3code.context_budget import FALLBACK_WINDOW, compact_threshold, family_window, has_explicit_window
from k3code.daemon import k3_home, socket_path
from k3code.halt import load_halt
from k3code.reliability.governor import read_psi
from k3code.reliability.journal import ToolJournal
from k3code.reliability.netwatch import NetWatchConfig, _default_provider_probe, default_internet_probe
from k3code.router.cooldown import clear_auth_cooldowns

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


async def _probe_provider(base_url: str, timeout: float = 5.0) -> tuple[bool, float | None, str]:
    start = time.monotonic()
    try:
        status, _body, _url = await _default_provider_probe(base_url, timeout)
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"[:120]
    ms = (time.monotonic() - start) * 1000
    return status < 500, ms, f"HTTP {status}"  # unauthenticated: 401/403 still means "reachable"


async def _probe_key(p: Any) -> tuple[bool, float | None, str]:
    """With a key: list the provider's models, so a rejected key fails here instead of on the first turn."""
    from k3code.setup import probe as setup_probe

    entry = {"kind": p.kind, "base_url": p.base_url}
    key = p.api_key or os.environ.get(p.api_key_env, "")
    ok, ms, _ids, detail = await asyncio.to_thread(setup_probe.list_models, entry, key, 8.0)
    if ok:
        return True, ms, detail
    if detail in ("HTTP 401", "HTTP 403"):
        return False, ms, f"{detail}: the API key ({p.api_key_env}) was rejected"
    if detail.startswith("HTTP 4"):  # reachable, but no models endpoint at this path: fall back to reachability
        return await _probe_provider(p.base_url)
    return False, ms, detail


async def _probe_claude_login() -> tuple[bool, float | None, str]:
    """`claude` on PATH is not a login. Only a Claude Code that says it is logged out fails; no answer is fine."""
    from k3code.providers import claude_cli

    logged_in = await asyncio.to_thread(claude_cli.login_status)
    return logged_in is not False, None, "not logged in" if logged_in is False else "local"


async def check_providers(config: Settings, probe: bool = True) -> list[Check]:
    checks: list[Check] = []
    if not config.providers:
        return [Check("providers", FAIL, "no providers configured", "run `k3code onboard` to add one")]
    results: list[tuple[bool, float | None, str]] = []
    if probe:
        results = await asyncio.gather(
            *(
                _probe_claude_login()
                if p.kind == "claude-cli"
                else _probe_key(p)  # a keyless entry (api_key_env "") lists its models without a key
                if p.api_key or not p.api_key_env or os.environ.get(p.api_key_env)
                else _probe_provider(p.base_url)
                for p in config.providers
            )
        )
    for i, p in enumerate(config.providers):
        if p.kind == "claude-cli":  # local binary, nothing to reach over HTTP
            path = shutil.which("claude")
            logged_out = bool(path and probe and not results[i][0])
            if not path:
                status, detail = FAIL, "Claude Code CLI not found on PATH"
                fix = "install Claude Code and run `claude` once to log in, or remove this entry"
            elif logged_out:
                status, detail, fix = WARN, f"Claude Code CLI at {path}, but not logged in", "run `claude`, then /login"
            else:
                status, detail, fix = OK, f"Claude Code CLI at {path}", ""
            checks.append(
                Check(f"provider:{p.name}", status, detail, fix, {"kind": "claude-cli", "command": path or ""})
            )
            continue
        if not probe:
            checks.append(Check(f"provider:{p.name}", OK, "probe skipped", data={"base_url": p.base_url}))
            continue
        ok, ms, detail = results[i]
        status = OK if ok else FAIL
        keyless = not (p.api_key or (p.api_key_env and os.environ.get(p.api_key_env)))
        if ok and not keyless:
            # the key answered: an auth cooldown armed by an earlier rejection is stale (the key was fixed in place)
            clear_auth_cooldowns(k3_home() / "cooldowns.json", provider=p.name)
        if ok and keyless and p.api_key_env:
            # only reachability was probed: an endpoint that answers 401 is up, but every call to it fails over
            checks.append(
                Check(
                    f"provider:{p.name}",
                    WARN,
                    f"reachable ({detail}, {ms:.0f} ms) but no key in {p.api_key_env}: calls to it fail over",
                    f"add {p.api_key_env}=<key> to ~/.config/k3code/env (mode 0600) or run "
                    "`k3code setup --step providers`",
                    {"base_url": p.base_url, "latency_ms": None if ms is None else round(ms, 1)},
                )
            )
            continue
        if ok and ms is not None and ms > 3000:
            status, detail = WARN, f"{detail}, slow ({ms:.0f} ms)"
        elif ok:
            detail = f"{detail}, {ms:.0f} ms"
        checks.append(
            Check(
                f"provider:{p.name}",
                status,
                detail,
                ""
                if ok
                else "set a new key with `k3code onboard`"
                if "rejected" in detail
                else f"check the network and {p.base_url}",
                {"base_url": p.base_url, "latency_ms": None if ms is None else round(ms, 1)},
            )
        )
    direct = [p.name for p in config.providers if not _is_omniroute(p.name, p.base_url)]
    if len(direct) == len(config.providers):  # no OmniRoute in the chain: nothing to say
        return checks
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


def check_context_windows(config: Settings) -> list[Check]:
    """Chain models whose context window is a guess: no ``models.<id>.context_window`` entry and no known family.

    Compaction keys to the window, so a wrong guess either compacts a large model too early or lets a small one
    overflow before compacting.
    """
    ids: list[str] = []
    for p in config.providers:
        for spec in [*p.models.values(), *p.tiers.values()]:
            for model_id in [spec] if isinstance(spec, str) else spec:
                if model_id and model_id not in ids:
                    ids.append(model_id)
    if not ids:
        return [Check("context-window", OK, "no chain models configured")]
    unknown = [m for m in ids if not has_explicit_window(config, m) and family_window(m) is None]
    if not unknown:
        return [Check("context-window", OK, "every chain model has a known context window")]
    return [
        Check(
            f"context-window:{m}",
            WARN,
            f"no known context window; assuming {FALLBACK_WINDOW} tokens "
            f"(compaction at ~{compact_threshold(config, m)})",
            f"set the model's real window in config.yaml: models: {{{m}: {{context_window: <tokens>}}}}",
            {"model": m, "assumed_window": FALLBACK_WINDOW},
        )
        for m in unknown
    ]


def check_prompt_cache(config: Settings) -> list[Check]:
    """Chain models of an openai-kind relay that ``prompt_cache: auto`` leaves without cache breakpoints because the id
    does not say Claude and no family is known (mostly a gateway alias in front of a large model)."""
    checks: list[Check] = []
    for p in config.providers:
        if p.kind != "openai" or p.prompt_cache != "auto" or httpx.URL(p.base_url).host == "api.openai.com":
            continue
        aliases: list[str] = []
        for spec in [*p.models.values(), *p.tiers.values()]:
            for model_id in [spec] if isinstance(spec, str) else spec:
                name = model_id.lower()
                if (
                    model_id
                    and model_id not in aliases
                    and "claude" not in name
                    and family_window(model_id) is None
                    and not any(fnmatch.fnmatchcase(name, pat.lower()) for pat in p.cache_models)
                ):
                    aliases.append(model_id)
        if aliases:
            checks.append(
                Check(
                    f"prompt-cache:{p.name}",
                    WARN,
                    f"{', '.join(aliases)}: no 'claude' in the id, so prompt_cache auto sends no cache breakpoints",
                    "if a Claude model sits behind it, set prompt_cache: on on this provider entry, or list the id "
                    "under cache_models: [pattern] to opt only these in",
                    {"provider": p.name, "models": aliases},
                )
            )
    return checks or [Check("prompt-cache", OK, "no relay alias ids without cache breakpoints")]


def check_keys(config: Settings) -> Check:
    # p.api_key is what load_config resolved from the process environment *or* ~/.config/k3code/env, where the setup
    # wizard stores keys: checking os.environ alone failed every shell run, and so did the update smoke test.
    missing = [
        p.api_key_env
        for p in config.providers
        # claude-cli uses the local login, and an entry with api_key_env "" (Ollama) declares that it takes no key
        if p.kind != "claude-cli" and p.api_key_env and not (p.api_key or os.environ.get(p.api_key_env))
    ]
    if not config.providers:
        return Check("api-keys", WARN, "no providers, nothing to check")
    if missing:
        fake = bool(os.environ.get("K3CODE_FAKE_PROVIDER"))
        # FAIL only when no chain entry can run: a keyless fallback behind a working entry is a warning
        usable = any(
            p.kind == "claude-cli" or not p.api_key_env or p.api_key or os.environ.get(p.api_key_env)
            for p in config.providers
        )
        return Check(
            "api-keys",
            WARN if fake or usable else FAIL,
            "missing env vars: " + ", ".join(sorted(set(missing))),
            "export them, or put them in ~/.config/k3code/env for the systemd unit",
            {"missing": sorted(set(missing))},
        )
    keyed = sum(1 for p in config.providers if p.kind != "claude-cli" and p.api_key_env)  # claude-cli, keyless: none
    return Check("api-keys", OK, f"{keyed} key env var(s) present" if keyed else "no provider needs a key")


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
        # Optional: the TUI starts its own gateway. The daemon only matters for /bg, cron and attach across terminals.
        return Check("daemon", OK, "not running (optional; `k3code daemon` keeps background work going)")
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
            reply = json.loads(line)
            if reply.get("id") == "doctor":
                break
        ms = (time.monotonic() - start) * 1000
        result = reply.get("result") or {}
        version = str(result.get("version") or "")
        data = {"socket": str(sock), "version": version}
        if result.get("background_paused"):  # restart-storm safe mode is in the daemon's memory only
            return Check(
                "daemon",
                WARN,
                f"answering on {sock} ({ms:.0f} ms) but background work is paused (restart-storm safe mode)",
                "fix what keeps restarting the daemon (journalctl --user -u k3code), then /daemon resume "
                "(or: k3code slash /daemon resume)",
                data,
            )
        return Check("daemon", OK, f"answering on {sock} ({ms:.0f} ms)", data=data)
    except (OSError, TimeoutError, ValueError, ConnectionError) as e:
        return Check("daemon", FAIL, f"not answering: {e}", "restart: systemctl --user restart k3code")
    finally:
        writer.close()


def check_halt(home: Path) -> Check:
    """``/daemon pause`` is persisted in ``halt.json``, so it is visible without the daemon (and with --no-probe) and it
    outlasts a daemon restart: until ``/daemon resume`` nothing reaches a provider."""
    halt = load_halt(home)
    if halt is None:
        return Check("daemon-paused", OK, "background work not halted")
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(halt.since)) if halt.since else "unknown time"
    return Check(
        "daemon-paused",
        WARN,
        f"background work is halted ({halt.reason}, since {since}): no turn, loop, job or sub-agent starts",
        "/daemon resume (or: k3code slash /daemon resume)",
        {"reason": halt.reason, "since": halt.since},
    )


def check_systemd() -> Check:
    if not service.is_installed():
        return Check("systemd-unit", OK, "not installed (optional)", "k3code service install")
    state = service.active_state()
    if state == "active":
        return Check("systemd-unit", OK, "k3code.service active")
    return Check(
        "systemd-unit", WARN, f"k3code.service is {state}", "systemctl --user start k3code; journalctl --user -u k3code"
    )


def check_env_file() -> Check:
    """The provider-key file the systemd unit loads: owner-only, and in the plain ``KEY=value`` form systemd parses."""
    from k3code.setup.state import env_file_path

    path = env_file_path()
    if not path.is_file():
        return Check("env-file", OK, f"{path} not present (optional)")
    problems: list[str] = []
    fixes: list[str] = []
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        problems.append(f"mode is {mode:04o}, expected 0600")
        fixes.append(f"chmod 600 {path}")
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as e:
        return Check("env-file", WARN, f"cannot read {path}: {e}", f"chmod 600 {path}")
    exported = [n for n, ln in enumerate(lines, 1) if ln.lstrip().startswith("export ")]
    if exported:
        problems.append(
            "systemd EnvironmentFile rejects 'export'; the daemon won't see those variables "
            f"(line {', '.join(map(str, exported[:5]))})"
        )
        fixes.append(f"remove the leading 'export ' in {path}")
    if problems:
        return Check("env-file", WARN, f"{path}: " + "; ".join(problems), "; ".join(fixes))
    return Check("env-file", OK, f"{path} mode 0600")


def check_linger() -> Check:
    """A user unit stops at logout unless lingering is on."""
    if not service.is_installed():
        return Check("linger", OK, "no systemd unit installed; nothing to keep running")
    import getpass

    user = getpass.getuser()
    if shutil.which("loginctl") is None:
        return Check("linger", OK, "loginctl not found; cannot tell")
    try:
        r = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger"], capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return Check("linger", OK, "loginctl did not answer; cannot tell")
    value = r.stdout.strip().partition("=")[2]
    if r.returncode != 0 or not value:
        return Check("linger", OK, "loginctl could not report lingering; cannot tell")
    if value == "yes":
        return Check("linger", OK, "lingering enabled; the daemon survives logout")
    return Check(
        "linger", WARN, "daemon stops at logout (lingering is off)", f"loginctl enable-linger {user} (needs sudo)"
    )


def check_daemon_version(running: str | None) -> Check:
    """A daemon started before an update keeps running the old code until it is restarted."""
    from k3code.paths import data_dir

    link = data_dir() / "current"
    if not running or not link.is_symlink():
        return Check("daemon-version", OK, "not comparable (daemon not running or no installed `current`)")
    name = link.resolve().name
    # a version dir is "<version>" or "<version>-src.<sha>"
    if name == running or name.startswith(f"{running}-"):
        return Check("daemon-version", OK, f"daemon runs {running}")
    return Check(
        "daemon-version",
        WARN,
        f"daemon runs {running} but the installed version is {name}",
        "restart the daemon: systemctl --user restart k3code",
        {"running": running, "installed": name},
    )


def check_install_links() -> Check:
    """``current`` must point at a version dir, and ``previous`` (the rollback target) must still exist."""
    from k3code.paths import data_dir

    data = data_dir()
    current = data / "current"
    if not current.is_symlink():
        return Check("install-links", OK, "no installed `current` (dev checkout or not installed)")
    if not current.exists():
        return Check(
            "install-links",
            WARN,
            f"`current` points at {os.readlink(current)}, which is missing",
            "re-run the installer, or point `current` at a directory in versions/",
        )
    previous = data / "previous"
    try:
        prev_name = previous.read_text().strip() if previous.is_file() else ""
    except OSError:
        prev_name = ""
    if prev_name and not (data / "versions" / prev_name).is_dir():
        return Check(
            "install-links",
            WARN,
            f"`previous` names version {prev_name}, which is missing: rollback is not possible",
            f"update again to refresh it, or remove {previous}",
        )
    return Check("install-links", OK, f"current -> {current.resolve().name}")


def find_repo_root() -> Path | None:
    from k3code.paths import install_roots

    for candidate in install_roots():
        if (candidate / "tui" / "package.json").is_file() or (candidate / "tui" / "dist" / "entry.js").is_file():
            return candidate
    return None


def check_tui() -> Check:
    from k3code.paths import MIN_NODE_MAJOR, NODE_ENV, resolve_node

    choice = resolve_node()
    root = find_repo_root()
    entry = root / "tui" / "dist" / "entry.js" if root else None
    fix = f"install Node.js >= {MIN_NODE_MAJOR}, run the k3code installer, or set {NODE_ENV}"
    if not choice.path:
        status = WARN if choice.old else FAIL
        return Check("tui", status, choice.reason, fix, {"node": choice.old.split(" ")[0]} if choice.old else {})
    used = f"node {choice.version} ({choice.reason}: {choice.path})"
    data = {"node": choice.version, "path": choice.path, "source": choice.source}
    if entry is None or not entry.is_file():
        # An install keeps only tui/dist, so with no sources there is no tui/ to build in. The installer keeps a
        # version it already built (`version X already installed`), so only an uninstall makes it build again.
        build_fix = (
            "cd tui && npm install && npm run build"
            if root
            else f"check Node.js >= {MIN_NODE_MAJOR} with npm and the network, run `sh install/uninstall.sh` (it keeps "
            "your data), then `sh install/install.sh` again; re-running the installer alone keeps this version as it is"
        )
        return Check("tui", WARN, f"{used}, TUI build missing", build_fix, data)
    if choice.major < MIN_NODE_MAJOR:  # only K3CODE_NODE can name a node that old
        return Check("tui", WARN, f"{used} is old", f"use Node.js >= {MIN_NODE_MAJOR} (or unset {NODE_ENV})", data)
    return Check("tui", OK, f"{used}, tui/dist/entry.js present", data=data)


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


#: Ubuntu 23.10+ (24.04 LTS) sets this to 1: an unprivileged program may create a user namespace only under an
#: AppArmor profile that allows ``userns``, and bwrap has none by default.
_APPARMOR_USERNS = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def _unusable_bwrap_fix(bwrap: str) -> str:
    try:
        restricted = _APPARMOR_USERNS.read_text().strip() == "1"
    except OSError:
        restricted = False
    if restricted:
        return (
            "Ubuntu restricts user namespaces with AppArmor. Allow bwrap: write these 3 lines to "
            "/etc/apparmor.d/bwrap: `abi <abi/4.0>,` `include <tunables/global>` "
            f"`profile bwrap {bwrap} flags=(unconfined) {{ userns, }}`, then `sudo apparmor_parser -r "
            "/etc/apparmor.d/bwrap`; or lift the restriction for every program: "
            "`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (persist it in /etc/sysctl.d/)"
        )
    return "allow unprivileged user namespaces for bwrap (run `bwrap --ro-bind / / true` to see why it fails)"


def check_sandbox(probe: bool = True) -> Check:
    from k3code.reliability import sandbox

    path = sandbox.bwrap_path()
    if path:
        # bwrap present is not enough: user namespaces may be blocked, and then every sandboxed command fails. The
        # self-test is local and cheap, so --no-probe (which skips the network) runs it too.
        if not sandbox.usable():
            return Check(
                "sandbox",
                WARN,
                "bubblewrap is installed but unusable (user namespaces blocked?); unattended bash is refused, "
                "interactive auto mode asks before each command it would have sandboxed, yolo bash runs unsandboxed",
                _unusable_bwrap_fix(path),
            )
        # bwrap starting is not enough either: a resolver file the sandbox cannot read means no host name resolves
        hidden = sandbox.hidden_name_lookup_files() if probe else []
        if hidden:
            return Check(
                "sandbox",
                WARN,
                f"bubblewrap at {path}, but sandboxed commands cannot read {', '.join(hidden)}: host name lookups "
                "there may fail (git fetch, pip/npm install, curl)",
                f"see where it links to (ls -l {hidden[0]}); the sandbox never shows a file under $HOME or the "
                "k3code home",
            )
        return Check("sandbox", OK, f"bubblewrap at {path}")
    return Check(
        "sandbox",
        WARN,
        "bwrap not found: unattended bash (background, goal, sub-agent) is refused; auto mode asks before each "
        "command it would have sandboxed; yolo bash runs WITHOUT a sandbox",
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


# The libraries a bare Debian/Ubuntu is missing for Chromium's headless shell (seen on a fresh Ubuntu 24.04 WSL), and
# the apt package that ships each. Any other missing library is left to Playwright, which keeps the full list.
_CHROMIUM_APT_LIBS = {
    "libnspr4.so": "libnspr4",
    "libnss3.so": "libnss3",
    "libnssutil3.so": "libnss3",
    "libasound.so.2": "libasound2",
}


def _missing_libraries(binary: Path) -> list[str]:
    """The shared libraries the loader cannot find for ``binary``, from ldd (the browser itself is not started)."""
    ldd = shutil.which("ldd")
    if ldd is None:
        return []
    try:
        r = subprocess.run([ldd, str(binary)], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.split("=>")[0].strip() for ln in r.stdout.splitlines() if ln.rstrip().endswith("=> not found")]


def _apt_package(name: str) -> str:
    """Ubuntu 24.04 renamed some libraries to ``<name>t64`` (libasound2 among them); older releases keep ``name``."""
    try:
        r = subprocess.run(["apt-cache", "show", name + "t64"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return name
    return name + "t64" if r.returncode == 0 else name


def _libraries_fix(missing: list[str]) -> str:
    if shutil.which("apt-get") is None:
        return f"install the packages that provide {', '.join(missing)} with your system package manager"
    packages = [_CHROMIUM_APT_LIBS.get(lib) for lib in missing]
    if None in packages:
        return f"sudo {shlex.quote(sys.executable)} -m playwright install-deps chromium-headless-shell"
    return "sudo apt-get install -y " + " ".join(_apt_package(p) for p in dict.fromkeys(packages) if p)


def check_browser() -> Check:
    """The optional browser tool: Playwright importable and Chromium where presetup puts it (local probe only)."""
    import importlib.util

    from k3code.paths import data_dir

    has_playwright = importlib.util.find_spec("playwright") is not None
    location = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or data_dir() / "browsers")
    has_chromium = location.is_dir() and any(location.glob("chromium*"))
    if has_playwright and has_chromium:
        # a directory is not a browser: look for the executable (never launched here); Playwright 1.63's headless
        # shell is chrome-headless-shell, older builds named it headless_shell
        names = ("chrome-headless-shell", "chrome-headless-shell.exe", "chrome", "headless_shell", "chrome.exe")
        binary = next(
            (
                p
                for d in location.glob("chromium*")
                for name in names
                for p in d.rglob(name)
                if p.is_file() and os.access(p, os.X_OK)
            ),
            None,
        )
        if binary is None:
            # the installer and a plain `playwright install` both skip a directory they already installed into
            return Check(
                "browser",
                WARN,
                f"Chromium directory at {location} has no executable browser; the browser tool is off",
                f"PLAYWRIGHT_BROWSERS_PATH={location} {sys.executable} -m playwright install"
                " --force --only-shell chromium",
            )
        missing = _missing_libraries(binary)
        if missing:
            return Check(
                "browser",
                WARN,
                f"Chromium at {location} cannot start: the system libraries {', '.join(missing)} are missing; "
                "the browser tool is off",
                _libraries_fix(missing),
                data={"path": str(location), "exe": str(binary), "missing_libraries": missing},
            )
        return Check(
            "browser", OK, f"Playwright and Chromium at {location}", data={"path": str(location), "exe": str(binary)}
        )
    if has_playwright or has_chromium:
        missing = "Chromium" if has_playwright else "Playwright"
        return Check(
            "browser", WARN, f"{missing} is missing; the browser tool is off", "re-run the installer without --minimal"
        )
    return Check(
        "browser",
        WARN,
        "not installed (optional: the browser tool needs Playwright and Chromium)",
        "re-run the installer without --minimal, or set K3CODE_SKIP_CHROMIUM only if you do not want it",
    )


def check_searxng() -> Check:
    """Web search through a SearXNG instance: the research setting, the legacy top-level key, or an MCP server."""
    from k3code import confio
    from k3code.paths import user_config_path

    raw = confio.read_yaml(user_config_path())
    url = str(((raw.get("research") or {}).get("searxng_url")) or ((raw.get("searxng") or {}).get("url")) or "")
    if url:
        return Check("searxng", OK, f"SearXNG at {url}", data={"url": url})
    servers = (raw.get("mcp") or {}).get("servers") or {}
    if any("searxng" in str(name).lower() for name in servers):
        return Check("searxng", OK, "a SearXNG MCP server is configured")
    return Check("searxng", OK, "not configured (optional: set research.searxng_url or connect a SearXNG MCP server)")


def check_project(project_dir: Path) -> Check:
    """The project's ``.k3code``: content left unloaded because it is not trusted, and providers it cannot set; else
    the stacks a read-only scan detects."""
    from k3code import confio, trust
    from k3code.learning import projectstate, recipes, stacks

    problems: list[str] = []
    if hint := trust.untrusted_hint(project_dir):
        problems.append(hint)
    cfg_path = trust.config_path(project_dir)
    if trust.project_dir_of(cfg_path) is not None and cfg_path.is_file():
        try:
            raw = confio.read_yaml(cfg_path)
        except Exception:  # noqa: BLE001 - an unreadable project config is reported by the trust prompt
            raw = {}
        if isinstance(raw, dict) and raw.get("providers"):
            problems.append(f"{cfg_path} sets providers: ignored (only your user config can set providers)")
        if isinstance(raw, dict) and raw.get("mem0"):
            problems.append(f"{cfg_path} sets mem0: ignored (only your user config can set mem0)")
    if problems:
        return Check("project", WARN, "; ".join(problems), "`k3code trust` shows what the project changes")
    # The bare trust state ("none") read as "no project detected". This is the scan a session's preparation runs,
    # read-only here: nothing is stored and nothing is proposed.
    found = [recipes.label(s) for s in stacks.scan(projectstate.project_root(project_dir)).stacks]
    detected = f"stacks: {', '.join(found)}" if found else "no stacks detected"
    decision = trust.decision(project_dir)
    config = "no project config" if decision == trust.NONE else f"project config {decision}"
    return Check("project", OK, f"{detected}; {config} ({project_dir})", data={"stacks": found})


def check_permissions(home: Path, project_dir: Path) -> Check:
    """Every ``permissions:`` entry k3code cannot read and enforces as a deny: the user config, the rules accepted from
    this project's recipe and the project config as trusted. The session logs each one once; this lists them all."""
    from k3code import trust
    from k3code.learning.projectstate import rules_path
    from k3code.permissions.state import parse_permissions, read_permissions

    problems: list[str] = []
    for path in (home / "config.yaml", rules_path(project_dir)):
        problems += [f"{path}: {p}" for p in read_permissions(path)[2]]
    if (text := trust.trusted_text(project_dir)) is not None:
        problems += [f"{trust.config_path(project_dir)}: {p}" for p in parse_permissions(text)[2]]
    if problems:
        fix = "fix each named entry (an action is allow, ask or deny, lower case); until then it is a deny"
        return Check("permissions", WARN, "; ".join(problems), fix, {"problems": len(problems)})
    return Check("permissions", OK, "every permissions entry is valid")


async def check_worktrees(project_dir: Path) -> Check:
    """Fan-out and sub-agent leftovers in this project's repo: worktrees whose process died and k3/* branches. A
    gateway start reaps them (``subagents.worktree.reap``); this only lists them."""
    from k3code.subagents import worktree

    found = await worktree.scan(project_dir)
    if found is None:
        return Check("worktrees", OK, "not in a git repository")

    def count(n: int, one: str, many: str) -> str:
        return f"{n} {one if n == 1 else many}"

    leftovers = [
        count(len(found.worktrees), "orphaned worktree", "orphaned worktrees") if found.worktrees else "",
        count(len(found.merged), "merged k3/* branch", "merged k3/* branches") if found.merged else "",
        count(len(found.expired), "expired k3/* branch", "expired k3/* branches") if found.expired else "",
    ]
    kept = count(len(found.unmerged), "unmerged k3/* branch", "unmerged k3/* branches") + " kept"
    data = {
        "worktrees": [str(w.path) for w in found.worktrees],
        "merged": found.merged,
        "expired": [name for name, _ in found.expired],
        "unmerged": found.unmerged,
    }
    if not any(leftovers):
        return Check("worktrees", OK, kept if found.unmerged else "no fan-out leftovers", data=data)
    detail = f"{', '.join(x for x in leftovers if x)} left by fan-out or sub-agents in {found.repo}"
    return Check(
        "worktrees",
        WARN,
        f"{detail}; {kept}" if found.unmerged else detail,
        "restart the daemon (it reaps them at start), or: git worktree remove --force <path>; git branch -D <branch>",
        data,
    )


def install_subset(home: Path | None = None) -> list[Check]:
    """What the installer prints after activation. Warnings only: no check here can fail an install.

    Providers, the daemon and the network probes are left out (a fresh install has none of them yet), and the
    sandbox is reported by the installer's own presetup step.
    """
    home = home or k3_home()
    checks = [check_home(home), check_disk(home), check_tui(), check_browser(), check_searxng()]
    return [Check(c.name, WARN if c.status == FAIL else c.status, c.detail, c.fix, c.data) for c in checks]


async def run_checks(
    config: Settings | None = None, *, probe: bool = True, network: bool = True, home: Path | None = None
) -> list[Check]:
    """``probe=False`` skips every probe; ``network=False`` skips only the ones that leave the machine (the providers,
    the internet), so the daemon and the sandbox are still tried."""
    home = home or k3_home()
    config = config or load_config(project_dir=Path.cwd())
    checks: list[Check] = []
    checks += await check_providers(config, probe=probe and network)
    checks.append(check_keys(config))
    checks += check_context_windows(config)
    checks += check_prompt_cache(config)
    checks.append(await check_netwatch_async() if probe and network else Check("netwatch", OK, "probe skipped"))
    checks += [check_disk(home), check_psi()]
    daemon_check = await check_daemon() if probe else Check("daemon", OK, "probe skipped")
    checks.append(daemon_check)
    checks += [
        check_halt(home),
        check_daemon_version(daemon_check.data.get("version")),
        check_systemd(),
        check_linger(),
        check_env_file(),
        check_install_links(),
        check_tui(),
        check_home(home),
        check_journal(home),
        check_vendor(),
        # the self-test runs bwrap (up to 10 s): off the event loop, since /doctor also runs inside the daemon
        await asyncio.to_thread(check_sandbox, probe),
        check_isolation(),
        check_permissions(home, default_project_dir()),
        await asyncio.to_thread(check_project, default_project_dir()),  # the stack scan reads the tree
        await check_worktrees(default_project_dir()),
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
