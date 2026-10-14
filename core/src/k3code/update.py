"""Versioned installs under ``<data>/versions/<ver>`` with an atomic ``current`` symlink, update and rollback."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from k3code import service
from k3code._version import recorded_build
from k3code.paths import GATEWAY_ENV_VARS, data_dir, home, user_config_path
from k3code.setup.state import env_file_path

DEFAULT_REPO = "K3NOXOFFICIAL/k3code"
#: The GitHub-compatible releases API; K3CODE_UPDATE_API or update.api point it at a mirror.
DEFAULT_API = "https://api.github.com"
KEEP_VERSIONS = 3
DAEMON_WAIT_SECONDS = 120
DAEMON_NOT_STARTED = "daemon not running, not started"
#: A unit file is installed but the systemd user manager does not answer (WSL without systemd, a container, `su`):
#: there is no daemon systemd could run, so the switch stands and only this is said (#115).
MANAGER_UNREACHABLE = (
    "warning: the systemd user manager is not reachable, so the daemon was not restarted "
    "(`k3code service uninstall` removes the unit on a box without systemd)"
)


def versions_dir() -> Path:
    return data_dir() / "versions"


def current_link() -> Path:
    return data_dir() / "current"


def previous_file() -> Path:
    return data_dir() / "previous"


def installed_versions() -> list[str]:
    d = versions_dir()
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


def current_version() -> str | None:
    link = current_link()
    return os.path.basename(os.readlink(link)) if link.is_symlink() else None


def previous_version() -> str | None:
    try:
        v = previous_file().read_text().strip()
    except OSError:
        return None
    return v if v and (versions_dir() / v).is_dir() else None


def stale_build() -> str | None:
    """The build this process runs when ``current`` names another one (it keeps the old code until restarted)."""
    running, installed = recorded_build(), current_version()
    return running if running and installed and running != installed else None


def stale_daemon_build() -> str | None:
    """The build the daemon reports on its socket when ``current`` names another one; None when it is not running.

    A daemon that predates build names reports the plain version (``0.1.0``) for any ``0.1.0-src.<sha>``: that is
    not comparable, so it is not called stale (doctor's daemon-version check makes the same call).
    """
    import asyncio

    from k3code import doctor

    installed = current_version()
    try:
        running = str(asyncio.run(doctor.check_daemon()).data.get("version") or "")
    except Exception:  # noqa: BLE001 - a daemon that does not answer is doctor's business, not the update's
        return None
    if not running or not installed or running == installed or installed.startswith(f"{running}-"):
        return None
    return running


def current_lines(cur: str | None, behind: str | None, who: str = "running: ") -> str:
    """``current: X``, and when a process still runs the build ``behind``, the line that says to restart."""
    if behind is None:
        return f"current: {cur}"
    how = f": systemctl --user restart {service.UNIT_NAME}" if service.is_installed() else ""
    return f"current: {cur}\n{who}{behind} (restart to use {cur}{how})"


def up_to_date(behind: str | None, text: str) -> str:
    """``text`` ("Already up to date."), unless a process still runs the build before the active one."""
    return "Nothing newer to install." if behind else text


def switch_to(version: str) -> None:
    """Atomically point ``current`` at ``versions/<version>`` and remember the old one as ``previous``."""
    target = versions_dir() / version
    if not target.is_dir():
        raise FileNotFoundError(f"no such version dir: {target}")
    old = current_version()
    link = current_link()
    tmp = link.with_name(".current.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, link)
    if old and old != version:
        previous_file().write_text(old + "\n")


def version_key(v: str) -> tuple[Any, ...]:
    """Sortable key: numeric core, a pre-release (``-dev.3``) sorts before the release.

    Pre-release identifiers compare as semver says: numeric ones as integers (``dev.10`` > ``dev.9``; as one string
    it was smaller), numeric before alphanumeric, and a shorter list first when it is a prefix of the other.
    """
    v = v.lstrip("v").split("+")[0]
    core, _, pre = v.partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.]", core))
    pre_key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return (nums, 0 if pre else 1, pre_key)


# -- smoke test / daemon health -------------------------------------------------


#: Doctor checks about the machine, not about the new version: they never block an update (a fresh install has no
#: providers yet, a node-less one no TUI); the daemon itself is health-checked after the switch.
ENV_CHECKS = (
    "provider",
    "providers",
    "api-keys",
    "omniroute-bypass",
    "netwatch",
    "disk",
    "psi",
    "daemon",
    "systemd-unit",
    "tui",
    "vendor",
    "sandbox",
    "hermes-isolation",
)


def smoke_test(vdir: Path, timeout: float = 60.0) -> tuple[bool, str]:
    """The new version's own venv: ``k3code --version`` names this version, and ``doctor --json --no-probe`` (which
    loads the config with the new code) has no failing check about k3code itself; environment checks do not count."""
    vdir = vdir.absolute()  # the children below run with cwd=vdir: a relative path or K3CODE_DATA would stop matching
    exe = vdir / "venv" / "bin" / "k3code"
    if not exe.exists():
        return False, f"{exe} missing"
    env = {**os.environ, "K3CODE_DATA": str(data_dir().absolute())}
    try:
        # cwd is the new version, not wherever the update started: doctor scans its cwd, and a drive root (/mnt/c)
        # holds entries it cannot read. A traceback names its cause last, so the message keeps the end of stderr.
        v = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=timeout, env=env, cwd=vdir, check=False
        )
        if v.returncode != 0:
            return False, f"--version failed: {v.stderr.strip()[-400:]}"
        want = vdir.name.split("-src")[0]
        if want not in v.stdout:
            return False, f"--version says {v.stdout.strip()[:80]!r}, expected {want}"
        d = subprocess.run(
            [str(exe), "doctor", "--json", "--no-probe"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=vdir,
            check=False,
        )
        try:
            report = json.loads(d.stdout)
        except ValueError:
            return False, f"doctor emitted no JSON (exit {d.returncode}): {d.stderr.strip()[-400:]}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    checks = report.get("checks") or []
    blocking = [
        str(c.get("name"))
        for c in checks
        if c.get("status") == "fail" and str(c.get("name", "")).split(":")[0] not in ENV_CHECKS
    ]
    if blocking or (not checks and report.get("summary", {}).get("fail", 0)):
        return False, f"doctor reports failing check(s): {', '.join(blocking) or 'unnamed'}"
    return True, v.stdout.strip()


def restart_daemon() -> None:
    """Restart the unit; raises ``service.ServiceError`` when systemd refuses (reported, never ignored)."""
    if service.is_installed():
        service.restart()


def daemon_healthy() -> bool:
    return service.active_state() == "active"


def daemon_is_stopped() -> bool:
    """True when systemd says the unit is ``inactive``, which is what ``systemctl --user stop`` leaves.

    A restart would start it, so an update or rollback leaves it alone (``install.sh`` does the same). ``failed`` (it
    crashed) and ``activating`` (it is waiting out an automatic restart) are not stopped: the restart is what brings
    them back. Without systemd the answer is no, and ``restart()`` reports that as before."""
    return service.active_state() == "inactive"


def wait_healthy(
    healthy: Callable[[], bool] = daemon_healthy,
    timeout: float = DAEMON_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    settle: float = 3.0,
) -> bool:
    """True once the daemon stays healthy for ``settle`` seconds, within ``timeout``."""
    deadline = clock() + timeout
    while clock() < deadline:
        if healthy():
            sleep(settle)
            if healthy():
                return True
        else:
            sleep(1.0)
    return False


@dataclass
class UpdateResult:
    ok: bool
    version: str
    message: str
    rolled_back: bool = False
    log: list[str] = field(default_factory=list)


def uv_path() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def carry_playwright(prev_vdir: Path, new_vdir: Path, uv: str | None = None) -> str | None:
    """Install the Playwright version of ``prev_vdir``'s venv into ``new_vdir``'s; a line for the log, or None.

    Only the installer's presetup installs Playwright, and every update builds a fresh venv without it, so the browser
    tool used to vanish on the first update. Chromium itself lives in ``<data>/browsers``, shared by all versions, so
    only the Python package is installed again (no browser download)."""
    old_py = prev_vdir / "venv" / "bin" / "python"
    new_py = new_vdir / "venv" / "bin" / "python"
    if not old_py.exists() or not new_py.exists():
        return None
    probe = "from importlib.metadata import version; import playwright; print(version('playwright'))"
    try:
        old = subprocess.run([str(old_py), "-c", probe], capture_output=True, text=True, timeout=60, check=False)
        if old.returncode != 0 or not old.stdout.strip():
            return None  # the browser tool was off before the update: keep it off
        ver = old.stdout.strip().splitlines()[-1]
        new = subprocess.run([str(new_py), "-c", probe], capture_output=True, text=True, timeout=60, check=False)
        if new.returncode == 0:
            return None  # the new version brings its own
        r = subprocess.run(
            [uv or uv_path(), "pip", "install", "--quiet", "--python", str(new_py), f"playwright=={ver}"],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"warning: could not carry Playwright over ({type(e).__name__}: {e}); the browser tool is off"
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()[-300:]
        return f"warning: could not install Playwright {ver} into the new version ({err}); the browser tool is off"
    return f"carried Playwright {ver} over to the new version"


def activate(
    version: str,
    *,
    restart: Callable[[], None] = restart_daemon,
    healthy: Callable[[], bool] = daemon_healthy,
    daemon_installed: Callable[[], bool] = service.is_installed,
    daemon_stopped: Callable[[], bool] = daemon_is_stopped,
    manager_reachable: Callable[[], bool] = service.manager_reachable,
    smoke: Callable[[Path], tuple[bool, str]] = smoke_test,
    wait: Callable[..., bool] = wait_healthy,
    carry: Callable[[Path, Path], str | None] = carry_playwright,
) -> UpdateResult:
    """Smoke-test ``versions/<version>``, switch to it, restart the daemon; roll back on any failure.

    A daemon that was stopped before the switch stays stopped: the new version is activated and nothing is started.

    Release, source and git updates all pass through here, so this is where the new venv gets the optional Playwright
    package the current one has (before the smoke test, which then runs the venv as it will be used)."""
    vdir = versions_dir() / version
    prev = current_version()
    log: list[str] = []
    if prev and prev != version and (carried := carry(versions_dir() / prev, vdir)):
        log.append(carried)
    ok, detail = smoke(vdir)
    if not ok:
        # a staged build that never ran: do not leave it behind. The installer reuses an installed build of the same
        # commit, so the staged dir can be one that a daemon or TUI still runs from: that one stays (#158).
        if version not in {prev, previous_version(), *running_versions()}:
            shutil.rmtree(vdir, ignore_errors=True)
        return UpdateResult(False, version, f"smoke test failed, not switching: {detail}", log=[*log, detail])
    stopped = daemon_installed() and daemon_stopped()  # asked before the switch, as the installer asks before its own
    switch_to(version)
    log.append(f"switched current -> {version} (was {prev})")
    if stopped:
        log.append(DAEMON_NOT_STARTED)
    elif daemon_installed():
        try:
            restart()
            up = wait(healthy)
        except service.ServiceError as e:
            log.append(str(e))
            # asked only once systemd refused: an unreachable manager means no daemon to restart, not an unhealthy one
            up = not manager_reachable()
            if up:
                log.append(MANAGER_UNREACHABLE)
        if not up:
            if prev:
                switch_to(prev)
                try:
                    restart()
                except service.ServiceError as e:
                    log.append(f"rolled back to {prev}, but the daemon did not restart: {e}")
                    return UpdateResult(False, version, log[-1], rolled_back=True, log=log)
                log.append(f"daemon unhealthy after {DAEMON_WAIT_SECONDS}s; rolled back to {prev}")
                return UpdateResult(False, version, log[-1], rolled_back=True, log=log)
            return UpdateResult(False, version, "daemon unhealthy and no previous version to roll back to", log=log)
    notes = "".join(f"\n{line}" for line in log if line.startswith("warning:") or line == DAEMON_NOT_STARTED)
    return UpdateResult(True, version, f"updated to {version}{notes}", log=log)


def rollback(
    *,
    restart: Callable[[], None] = restart_daemon,
    daemon_installed: Callable[[], bool] = service.is_installed,
    daemon_stopped: Callable[[], bool] = daemon_is_stopped,
    manager_reachable: Callable[[], bool] = service.manager_reachable,
) -> UpdateResult:
    """Switch back to the previous version, under the install lock: an update that is building (or install.sh) would
    otherwise switch ``current`` again once it is done and silently undo the rollback.

    A daemon that was stopped before the switch stays stopped."""
    try:
        with install_lock():
            prev = previous_version()
            cur = current_version()
            if not prev:
                return UpdateResult(False, cur or "", "no previous version to roll back to")
            stopped = daemon_installed() and daemon_stopped()
            switch_to(prev)  # records `cur` as the new previous, so rollback twice toggles
            if stopped:
                return UpdateResult(True, prev, f"rolled back {cur} -> {prev}\n{DAEMON_NOT_STARTED}", rolled_back=True)
            if daemon_installed():
                try:
                    restart()
                except service.ServiceError as e:
                    if not manager_reachable():
                        msg = f"rolled back {cur} -> {prev}\n{MANAGER_UNREACHABLE}"
                        return UpdateResult(True, prev, msg, rolled_back=True)
                    return UpdateResult(False, prev, f"rolled back {cur} -> {prev}, but {e}", rolled_back=True)
            return UpdateResult(True, prev, f"rolled back {cur} -> {prev}", rolled_back=True)
    except InstallLockHeld as e:
        return UpdateResult(False, current_version() or "", f"an update is running, not rolling back: {e}")


#: What shows that a version dir has the TUI (the file doctor's check_tui looks for) and the k3 multi-window binary.
#: The installer builds both on a best-effort basis, so a version without one is complete, just poorer.
TUI_ENTRY = Path("tui") / "dist" / "entry.js"
K3_BINARY = Path("bin") / "k3"
#: install.sh writes ``k3: <why>`` here when it tried to build the k3 binary and failed (a skip leaves nothing).
DEGRADED_FILE = ".degraded"


def has_file(version: str, rel: Path) -> bool:
    return (versions_dir() / version / rel).is_file()


def newest_first(versions: list[str]) -> list[str]:
    """``versions`` by build time (``.complete``, else the dir), newest first.

    Not by name: the ``-src.<sha>`` suffix of a git or source build compares as a plain string, which says nothing
    about age (prune kept an old build and deleted a newer one). Equal times fall back to the version number."""

    def built_at(version: str) -> float:
        d = versions_dir() / version
        for p in (d / ".complete", d):
            with contextlib.suppress(OSError):
                return p.stat().st_mtime
        return 0.0

    return sorted(versions, key=lambda v: (built_at(v), version_key(v)), reverse=True)


def running_versions(proc: Path = Path("/proc")) -> set[str]:
    """The ``versions/<name>`` dirs that a running process executes from or sits in (needs /proc: empty elsewhere).

    A venv python's ``exe`` is the base interpreter, so the version shows in the command line (the script and the
    shebang interpreter), as install.sh's daemon_version reads it; the TUI's node and gateway processes name it the
    same way. What cannot be read (another user's process, one that just exited) is skipped."""
    base = versions_dir()
    prefixes = "|".join(re.escape(f"{p}/") for p in {str(base), str(base.resolve())})
    name_after_prefix = re.compile(f"(?:{prefixes})([^/\\s]+)")
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return set()
    found: set[str] = set()
    for pid in pids:
        texts: list[str] = []
        for link in ("exe", "cwd"):
            with contextlib.suppress(OSError):
                texts.append(os.readlink(pid / link))
        with contextlib.suppress(OSError):
            texts.append((pid / "cmdline").read_bytes().replace(b"\0", b"\n").decode(errors="replace"))
        found.update(m.group(1) for text in texts for m in name_after_prefix.finditer(text))
    return found


def prune(keep: int = KEEP_VERSIONS) -> list[str]:
    """Delete the version dirs beyond the ``keep`` newest builds, oldest first by build time.

    Never the current or previous one, one that a running process executes from (a daemon or TUI that lost its
    directory fails on its next lazy import), nor, when the current version has no k3 binary, the newest version
    that still has one (a rollback or a retried build needs it)."""
    cur = current_version()
    protected = {cur, previous_version(), *running_versions()}
    vers = newest_first(installed_versions())
    if cur and not has_file(cur, K3_BINARY):
        protected.add(next((v for v in vers if has_file(v, K3_BINARY)), None))
    removed = []
    for v in vers[keep:]:
        if v not in protected:
            shutil.rmtree(versions_dir() / v, ignore_errors=True)
            removed.append(v)
    return removed


# -- release discovery ----------------------------------------------------------


@dataclass
class Release:
    tag: str
    version: str
    body: str
    prerelease: bool
    assets: dict[str, str]  # name -> API url


def github_token() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    if shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def update_settings() -> dict[str, Any]:
    from k3code import confio

    s = confio.read_yaml(user_config_path()).get("update") or {}
    repo = s.get("repo", DEFAULT_REPO)
    # the git remote a `install.sh --from-git` install updates from: update.url, else update.repo, else the remote
    # the install was made from (a fork or mirror), else the default repository
    url = s.get("url") or (None if "repo" in s else git_url())
    return {
        "channel": s.get("channel", "stable"),
        "repo": repo,
        "source": s.get("source", ""),
        "url": url or f"https://github.com/{repo}.git",
        "api": os.environ.get("K3CODE_UPDATE_API") or s.get("api") or DEFAULT_API,
    }


def _headers(token: str | None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_latest(
    channel: str = "stable", repo: str = DEFAULT_REPO, token: str | None = None, api: str | None = None
) -> Release | None:
    """Newest release on ``channel`` (``stable`` skips prereleases; ``dev`` takes anything)."""
    base = (api or os.environ.get("K3CODE_UPDATE_API") or DEFAULT_API).rstrip("/")
    r = httpx.get(f"{base}/repos/{repo}/releases?per_page=30", headers=_headers(token), timeout=15)
    if r.status_code == 429 or (r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"):
        wait = r.headers.get("retry-after")
        raise RuntimeError(
            f"GitHub API {r.status_code}: rate limit exceeded"
            + (f" (retry after {wait}s)" if wait else "")
            + "; set GITHUB_TOKEN (or `gh auth login`) for a higher limit"
        )
    if r.status_code in (401, 403, 404):
        raise PermissionError(
            f"GitHub API {r.status_code}: {repo} is private or does not exist; set GITHUB_TOKEN (or `gh auth login`) "
            "to read its releases"
        )
    r.raise_for_status()
    rels = [
        Release(
            tag=x["tag_name"],
            version=x["tag_name"].lstrip("v"),
            body=x.get("body") or "",
            prerelease=bool(x.get("prerelease")),
            assets={a["name"]: a["url"] for a in x.get("assets", [])},
        )
        for x in r.json()
        if not x.get("draft")
    ]
    if channel == "stable":
        rels = [x for x in rels if not x.prerelease]
    return max(rels, key=lambda x: version_key(x.version), default=None)


def _download(url: str, dest: Path, token: str | None) -> None:
    with httpx.stream(
        "GET", url, headers=_headers(token, "application/octet-stream"), follow_redirects=True, timeout=120
    ) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


def _sha256(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


def is_newer(candidate: str, current: str | None) -> bool:
    """True when ``candidate`` is newer than the installed ``current`` (``X.Y.Z-src.<sha>`` counts as ``X.Y.Z``)."""
    if not current:
        return True
    return version_key(candidate) > version_key(current.split("-src")[0])


class IntegrityError(ValueError):
    """A release asset could not be verified: nothing from the release is installed."""


def verify_checksums(files: dict[str, Path]) -> None:
    """Fail closed: SHA256SUMS must be there and list every other downloaded asset with a matching hash."""
    if "SHA256SUMS" not in files:
        raise IntegrityError("the release has no SHA256SUMS: refusing to install files that cannot be verified")
    sums = {}
    for line in files["SHA256SUMS"].read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0].lower()
    for name, path in files.items():
        if name == "SHA256SUMS":
            continue
        if name not in sums:
            raise IntegrityError(f"SHA256SUMS has no entry for {name}: refusing to install it unverified")
        if sums[name] != _sha256(path):
            raise IntegrityError(f"checksum mismatch for {name}: the download is corrupt or was altered")


class UnpinnedReleaseError(IntegrityError):
    """The release ships no locked requirements file: its dependencies would be whatever PyPI has today."""


#: The release asset with the runtime dependencies exported from core/uv.lock, with hashes (``k3code-<ver>-...``).
REQUIREMENTS_SUFFIX = "-requirements.txt"
#: install/install.sh's lock in the install root: a directory holding the pid of the run that owns it (``pid``), when
#: that process started (``start``, see _process_start) and the temp paths install.sh is using (``<kind>_tmp``, for
#: each of RECORDED_TMP_KINDS).
INSTALL_LOCK = ".install.lock"
RECORDED_TMP_KINDS = ("tui", "src", "giterr", "uv", "node", "go", "reqs")


class InstallLockHeld(OSError):
    """Another install or update into the same install root holds its lock."""


class ReleaseBuildError(RuntimeError):
    """A ``uv`` step of building a release failed; the message ends with the last lines of uv's own output."""


def _run_uv(cmd: list[str], what: str) -> None:
    """Run one ``uv`` step of a release build; a failure carries the end of uv's output (the cause comes last)."""
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        tail = "\n".join(((e.stderr or "").strip() or (e.stdout or "").strip()).splitlines()[-8:])
        raise ReleaseBuildError(f"{what} failed (exit {e.returncode}):\n{tail}") from e


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # another user's process
        return True
    return True


def _stat_start(stat: bytes) -> str:
    """Field 22 (start time) of a /proc/<pid>/stat line. The command name (field 2) may hold spaces and ")", so the
    fields are counted from the last ")"."""
    fields = stat.rpartition(b")")[2].split()
    return fields[19].decode() if len(fields) > 19 else ""


def _process_start(pid: int) -> str:
    """When ``pid`` started, to tell it from a later process given the same pid; "" when unknown.

    install.sh's proc_start writes and compares the same value, so each understands the other's lock: field 22 of
    /proc/<pid>/stat where there is a /proc, else (macOS) ``ps -o lstart=`` in a fixed locale and time zone."""
    if Path("/proc/self").is_dir():
        try:
            return _stat_start(Path(f"/proc/{pid}/stat").read_bytes())
        except OSError:
            return ""
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    try:
        ps = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, env=env, check=False
        )
    except OSError:
        return ""
    return " ".join(ps.stdout.split())


def _remove_recorded_tmp(lock: Path) -> None:
    """Remove the temp paths a killed install.sh recorded in ``lock`` (``<kind>_tmp`` holds the ``k3code-<kind>.*`` it
    made). Never an arbitrary path from a lock file: only one named for its kind, directly in the temp dir install.sh
    uses."""
    tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    for kind in RECORDED_TMP_KINDS:
        try:
            recorded = Path((lock / f"{kind}_tmp").read_text().rstrip("\n"))
        except OSError:
            continue
        prefix = f"k3code-{kind}."
        if recorded.parent != tmp or not recorded.name.startswith(prefix) or recorded.name == prefix:
            continue
        if recorded.is_dir() and not recorded.is_symlink():
            shutil.rmtree(recorded, ignore_errors=True)
        else:
            recorded.unlink(missing_ok=True)
        if kind == "giterr":  # the ref list read without git
            recorded.with_name(recorded.name + ".refs").unlink(missing_ok=True)


def _take_over_stale_lock(lock: Path) -> None:
    """Take ``lock`` (which exists) if the process it names is gone; InstallLockHeld otherwise.

    A pid is reused, so a live pid is still the lock's owner only if it started when the lock says. A lock without a
    start time (an older installer), or a start time that cannot be read here, trusts the pid alone."""
    try:
        pid = int((lock / "pid").read_text().strip())
    except (OSError, ValueError):
        raise InstallLockHeld(
            f"an install lock without a pid is in {lock} (an install that is just starting?): if no other "
            f"install runs, remove it (rm -r '{lock}') and retry"
        ) from None
    if _pid_running(pid):
        try:
            started = (lock / "start").read_text().strip()
        except OSError:
            started = ""
        now = _process_start(pid) if started else ""
        if not now or now == started:
            raise InstallLockHeld(f"another install into {lock.parent} is running (pid {pid}); wait for it to finish")
    _remove_recorded_tmp(lock)
    # install.sh builds in <root>/tmp (stage_dir) and only while it holds the lock: what is left there is the dead run's
    shutil.rmtree(lock.parent / "tmp", ignore_errors=True)
    # Not atomic, as in install.sh's take_lock: two runs that find the same stale lock can interleave (one removes it
    # and makes its own, the other then removes that one) and both go on; only a run that arrives between the rmtree
    # and the mkdir is stopped, by the mkdir. It takes two installs started within the same few milliseconds over a
    # lock a killed install left, so it is documented here rather than closed with a retry.
    shutil.rmtree(lock, ignore_errors=True)
    try:
        lock.mkdir()
    except FileExistsError:
        raise InstallLockHeld(f"another install into {lock.parent} took the lock just now") from None


@contextlib.contextmanager
def install_lock() -> Iterator[None]:
    """Hold the install root's ``.install.lock``, the one install.sh takes, while building a version.

    install.sh removes every ``versions/<v>`` without ``.complete`` when it takes over a stale lock, and this build
    removes an unfinished ``versions/<ver>`` itself, so the two must not build at the same time. A lock whose process
    is gone (a killed install or update, also when its pid now names a later process) is taken over, as install.sh
    does, with the temp paths it recorded; a lock with a live pid, or without a pid (an install that is just
    starting), refuses."""
    lock = data_dir() / INSTALL_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        _take_over_stale_lock(lock)
    try:
        # the start time first: a kill between the two writes must not leave a pid without it (a recycled pid would
        # then keep the lock alive)
        (lock / "start").write_text(f"{_process_start(os.getpid())}\n")
        (lock / "pid").write_text(f"{os.getpid()}\n")
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def install_release(rel: Release, token: str | None, uv: str | None = None, *, allow_unpinned: bool = False) -> Path:
    """Download + verify the release assets and build ``versions/<ver>`` (not yet activated).

    The dependencies come from the release's hashed requirements file (the set CI tested, from core/uv.lock), the
    wheel itself with ``--no-deps``: installing the wheel alone resolved the newest PyPI versions of everything. A
    release without that file is refused unless ``allow_unpinned``.

    Never touches a version that is installed and complete, nor the current or previous one: ``/update now`` used
    to rmtree ``versions/<ver>`` even when it was the live install, deleting the running k3code. The build holds
    install.sh's lock (see install_lock); InstallLockHeld while another install runs.
    """
    with install_lock():
        return _build_release(rel, token, uv, allow_unpinned=allow_unpinned)


def _build_release(rel: Release, token: str | None, uv: str | None, *, allow_unpinned: bool) -> Path:
    uv = uv or uv_path()
    vdir = versions_dir() / rel.version
    if vdir.exists():
        if (vdir / ".complete").is_file():
            return vdir  # already installed: nothing to download or rebuild
        if rel.version in (current_version(), previous_version()):
            raise ValueError(f"refusing to rebuild {rel.version}: it is the active or the previous version")
        shutil.rmtree(vdir)  # an interrupted earlier download
    if not allow_unpinned and not any(n.endswith(REQUIREMENTS_SUFFIX) for n in rel.assets):
        raise UnpinnedReleaseError(
            f"release {rel.version} has no locked requirements file (k3code-{rel.version}{REQUIREMENTS_SUFFIX}), so "
            "its dependencies would be resolved from PyPI as they are today rather than the set it was tested "
            "with. Run `k3code update --allow-unpinned` to install it anyway"
        )
    # Build in place: venvs are not relocatable (absolute shebangs). `.complete` is written last.
    vdir.mkdir(parents=True)
    try:
        dl = vdir / ".dl"
        dl.mkdir()
        files: dict[str, Path] = {}
        for name, url in rel.assets.items():
            files[name] = dl / name
            _download(url, files[name], token)
        verify_checksums(files)
        wheel = next((p for n, p in files.items() if n.endswith(".whl")), None)
        if wheel is None:
            raise ValueError("release has no wheel")
        _run_uv([uv, "venv", "--python", ">=3.12", str(vdir / "venv")], "uv venv")
        py = str(vdir / "venv" / "bin" / "python")
        reqs = next((p for n, p in files.items() if n.endswith(REQUIREMENTS_SUFFIX)), None)
        if reqs is None:  # allow_unpinned: an older release, resolved as before
            _run_uv([uv, "pip", "install", "--python", py, str(wheel)], "uv pip install")
        else:
            pip = [uv, "pip", "install", "--python", py]
            _run_uv([*pip, "--require-hashes", "-r", str(reqs)], "uv pip install")
            _run_uv([*pip, "--no-deps", str(wheel)], "uv pip install")
        tui = next((p for n, p in files.items() if n.startswith("k3code-tui") and n.endswith(".tar.gz")), None)
        if tui:
            (vdir / "tui").mkdir()
            with tarfile.open(tui) as t:
                t.extractall(vdir / "tui", filter="data")
        import platform

        arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), platform.machine())
        k3 = files.get(f"k3-{sys.platform}-{arch}")  # linux or darwin
        if k3:
            (vdir / "bin").mkdir()
            shutil.copy2(k3, vdir / "bin" / "k3")
            (vdir / "bin" / "k3").chmod(0o755)
        shutil.rmtree(dl)
        (vdir / ".complete").write_text(rel.version + "\n")
    except BaseException:
        shutil.rmtree(vdir, ignore_errors=True)
        raise
    return vdir


def update_log_path() -> Path:
    return data_dir() / "update.log"


def spawn_update(exe: str) -> str:
    """Run ``k3code update --yes`` as a process of its own session, logging to ``update.log``.

    Used when no service unit is installed (macOS, WSL without systemd, a TUI without the daemon): nothing has to be
    restarted, so the update only has to outlive this process and the terminal it runs in."""
    log_path = update_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in GATEWAY_ENV_VARS}
    try:
        with log_path.open("ab") as log:
            log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} k3code update --yes ====\n".encode())
            log.flush()
            p = subprocess.Popen(
                [exe, "update", "--yes"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
    except OSError as e:
        return f"Could not start the update: {e}. Run `k3code update --yes` from a terminal."
    return (
        f"Update started in the background (pid {p.pid}). The new version is smoke-tested and only switched to when it "
        f"passes; restart k3code afterwards to use it. Follow it in {log_path}. On WSL keep a k3code window open "
        "until it is done: WSL may stop the distribution, and the update with it, once no window is open."
    )


def apply_detached(*, unit_installed: Callable[[], bool] = service.is_installed) -> str:
    """Run ``k3code update --yes`` outside this process's service unit, so the update can restart the unit.

    The daemon used to install, restart its own unit from a worker thread and then poll for health *inside* the unit
    it was restarting: systemd SIGTERMed the whole cgroup (the poller included), so the auto-rollback branch could
    never run and the shutdown hung on the executor thread. A transient ``systemd-run --user`` unit survives it.
    Without a unit there is nothing to restart, and no systemd to ask on macOS or most WSL setups: a detached process.

    A transient unit starts from the user manager's environment, not ours. The daemon's unit hands it K3CODE_DATA and
    K3CODE_HOME (``Environment=``) and the provider and GitHub keys (``EnvironmentFile=``); the update gets the same,
    so it updates the install the daemon runs from and can reach a private repository. The keys stay in the file:
    nothing secret goes on a command line or into the unit's properties.
    """
    exe = shutil.which("k3code") or str(Path(sys.argv[0]).resolve())
    if not unit_installed():
        return spawn_update(exe)
    runner = shutil.which("systemd-run")
    if runner is None:
        return (
            "systemd-run is not available: run `k3code update --yes` from a terminal "
            "(the daemon cannot safely update the unit it is running in)."
        )
    unit = f"k3code-update-{int(time.time())}"
    r = subprocess.run(
        [
            runner,
            "--user",
            "--collect",
            "--quiet",
            f"--unit={unit}",
            f"--setenv=K3CODE_DATA={data_dir()}",
            f"--setenv=K3CODE_HOME={home()}",
            f"--property=EnvironmentFile=-{env_file_path().absolute()}",
            exe,
            "update",
            "--yes",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if r.returncode != 0:
        return f"Could not start the update: {(r.stderr or r.stdout).strip()[:300]}"
    return (
        f"Update started in the background ({unit}). The daemon restarts when it is installed and smoke-tested; "
        f"it is rolled back automatically if the new version does not come up. Follow it with "
        f"`journalctl --user -u {unit}`."
    )


def source_checkout() -> Path | None:
    s = update_settings()["source"]
    if s:
        return Path(s).expanduser()
    f = data_dir() / "source_path"
    return Path(f.read_text().strip()) if f.is_file() else None


class SourceUpdateError(RuntimeError):
    """The source checkout could not be updated; the message says what to do about it."""


#: What git prints when it needs credentials it does not have (private repository, no helper, prompts disabled).
_AUTH_HINTS = (
    "could not read username",
    "authentication failed",
    "terminal prompts disabled",
    "permission denied (publickey)",
    "repository not found",
    "invalid username or password",
)


def _in_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def installer_command(args: str) -> str:
    """The installer command for a hint, in backticks. On Windows k3code runs in WSL, where the installer a user
    starts from is `install.ps1` (there is no `sh` in PowerShell); `install.sh` still works inside WSL."""
    sh = f"`sh install.sh {args}`"
    if not _in_wsl():
        return sh
    return f"`powershell -ExecutionPolicy Bypass -File install\\install.ps1 {args}` or, inside WSL, {sh}"


def git_pull_command(checkout: Path) -> list[str]:
    """The command that fast-forwards ``checkout``.

    A checkout on a Windows drive (``/mnt/c/...``) seen from WSL was cloned with Windows git, which holds the user's
    GitHub credentials. WSL's own git has none and cannot pull a private repository, so use ``git.exe`` there."""
    path = str(checkout)
    if _in_wsl() and re.match(r"^/mnt/[a-z]/", path) and (exe := shutil.which("git.exe")):
        win = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, check=False).stdout.strip()
        if win:
            return [exe, "-C", win, "pull", "--ff-only"]
    # safe.directory: a checkout owned by another user is "dubious" and git refuses it (install.sh trusts it alike)
    # (both spellings: git before 2.46 compares the value with the symlink-free path)
    real = str(checkout.resolve())
    return ["git", "-c", f"safe.directory={path}", "-c", f"safe.directory={real}", "-C", path, "pull", "--ff-only"]


def pull_checkout(checkout: Path) -> None:
    """``git pull --ff-only`` in ``checkout``; fails with advice instead of a traceback or a hung credential prompt."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(git_pull_command(checkout), capture_output=True, text=True, check=False, env=env)
    if r.returncode == 0:
        return
    err = (r.stderr or r.stdout or "").strip()
    if any(hint in err.lower() for hint in _AUTH_HINTS):
        last = err.splitlines()[-1][:140] if err else ""
        raise SourceUpdateError(
            f"git could not reach GitHub from here ({last}).\n"
            f"Pull the checkout yourself with a git that has your credentials (on Windows: in PowerShell, "
            f"`cd` into the clone and run `git pull`), then run `k3code update --from-source --no-pull`."
        )
    raise SourceUpdateError(f"git pull failed in {checkout}:\n{err[:600]}")


#: How much of a failed installer's output an error shows. npm names its reason in the first lines of a stack trace
#: of about thirty, so the last few lines alone would show only the trace.
INSTALLER_TAIL_LINES = 40


def _installer_env(**extra: str) -> dict[str, str]:
    """The environment for install.sh: an install made as root (``--allow-root``) must stay updatable as root, and a
    TUI build that fails (npm unreachable) must fail the staging instead of leaving a version without the TUI to
    switch to. An environment variable, unlike a flag, is ignored by an older installer (see vet_staged)."""
    env = {**os.environ, "K3_REQUIRE_TUI": "1", **extra}
    if os.geteuid() == 0:
        env["K3_ALLOW_ROOT"] = "1"
    return env


def vet_staged(version: str) -> list[str]:
    """Compare ``version``, staged and not yet active, with the active one; the warning lines to show after the switch.

    The installer builds the TUI and the k3 binary on a best-effort basis, so a build that lost one (npm or the Go
    module proxy unreachable) is a complete version, and an update used to switch to it and report success.

    A TUI that the active version has and ``version`` lacks refuses the update (``SourceUpdateError``): the staged dir
    is removed, unless it is the previous version or one that a process runs from (the installer reuses an installed
    build of the same commit), so that the next update builds it again instead of finding it installed. install.sh
    fails that build itself under K3_REQUIRE_TUI; this covers a ref whose installer predates it and a TUI that was
    skipped (Node could not be fetched). k3 is optional, so a missing one only warns."""
    cur = current_version()
    if not cur or cur == version:
        return []
    if has_file(cur, TUI_ENTRY) and not has_file(version, TUI_ENTRY):
        if version not in {previous_version(), *running_versions()}:
            shutil.rmtree(versions_dir() / version, ignore_errors=True)
        raise SourceUpdateError(
            f"{version} was not activated: its TUI is missing (the build failed or was skipped; the installer's "
            f"messages are in {data_dir() / 'install.log'}) and {cur} stays current. Run `k3code update` again when "
            "the TUI can build."
        )
    if has_file(version, K3_BINARY):
        return []
    try:
        failed = any(
            line.startswith("k3:") for line in (versions_dir() / version / DEGRADED_FILE).read_text().splitlines()
        )
    except OSError:
        failed = False
    holder = next((v for v in newest_first(installed_versions()) if v != version and has_file(v, K3_BINARY)), None)
    if holder is None and not failed:
        return []  # nothing was lost and nothing is known to have failed (no Go on this machine)
    kept = "no installed version has it" if holder is None else f"{holder} still has it"
    if holder == cur:
        kept += "; `k3code update --rollback` goes back to it"
    why = "its build failed" if failed else "it was not built"
    log = data_dir() / "install.log"
    return [f"warning: {version} has no k3 multi-window binary ({why}; the installer's messages are in {log}): {kept}"]


#: What install.sh puts in front of its own progress lines ("installing the k3code core", "building the TUI ...").
INSTALLER_PREFIX = "k3code-install:"


def _show_progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _run_installer(
    cmd: list[str], *, env: dict[str, str], progress: Callable[[str], None], stdin: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installer with its output captured, handing its own progress lines to ``progress`` as they appear.

    A build takes 13-25 s warm and minutes cold, and ``capture_output`` kept all of it back until the end. The tools it
    runs (npm, uv, go) write a lot more to the same stderr; that is only kept, for the failure message."""
    rfd, wfd = os.pipe()
    lines: list[str] = []

    def pump() -> None:
        with os.fdopen(rfd, encoding="utf-8", errors="replace") as f:
            for line in f:
                lines.append(line)
                if line.startswith(INSTALLER_PREFIX):
                    # keep reading whatever the display does: a pump that stops lets the pipe fill and blocks the build
                    with contextlib.suppress(Exception):
                        progress(line.rstrip("\n"))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        r = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=wfd, text=True, stdin=stdin, env=env)
    finally:
        os.close(wfd)
        reader.join()
    return subprocess.CompletedProcess(r.args, r.returncode, r.stdout, "".join(lines))


def update_from_source(checkout: Path, *, pull: bool = True, progress: Callable[[str], None] = _show_progress) -> str:
    """``git pull --ff-only`` then stage a new version via the installer (``--no-activate``); returns its name.

    The installer's progress lines go to ``progress`` (stderr by default) while it builds."""
    if pull:
        pull_checkout(checkout)
    r = _run_installer(
        [
            "sh",
            str(checkout / "install" / "install.sh"),
            "--from-source",
            "--yes",
            "--no-setup",
            "--no-activate",
            "--print-version",
        ],
        env=_installer_env(),
        progress=progress,
    )
    if r.returncode:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-INSTALLER_TAIL_LINES:])
        raise SourceUpdateError(f"the installer failed (exit {r.returncode}):\n{tail}")
    return r.stdout.strip().splitlines()[-1]


# -- installs made with `install.sh --from-git` -----------------------------------
# Such an install keeps no checkout: only `<version dir>/.ref`, the branch or tag it was built from. Its version is
# named `<VERSION>-src.<short sha>`, so the remote head of that ref tells whether there is anything newer.

LS_REMOTE_TIMEOUT = 30.0
FETCH_TIMEOUT = 300.0

#: What git prints when the network is down (the same patterns install.sh uses to tell offline from private).
_OFFLINE_HINTS = re.compile(
    r"could not resolve host|temporary failure in name resolution|failed to connect|network is unreachable"
    r"|connection (timed out|refused|reset)|operation timed out",
    re.IGNORECASE,
)


def git_ref() -> str | None:
    """The ref the active version was built from (``install.sh --from-git``); None for any other install."""
    try:
        ref = (current_link() / ".ref").read_text().strip()
    except OSError:
        return None
    return ref or None


def git_url() -> str | None:
    """The remote the active version was built from (``install.sh --from-git``); None when it was not recorded."""
    try:
        url = (current_link() / ".url").read_text().strip()
    except OSError:
        return None
    return url or None


def is_commit_sha(ref: str) -> bool:
    """A full commit SHA: an install of it is pinned and never moves."""
    return re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref) is not None


def installed_sha() -> str | None:
    """The commit the active version was built from: the ``<sha>`` of ``X.Y.Z-src.<sha>``."""
    m = re.search(r"-src\.([0-9a-f]{7,64})$", current_version() or "")
    return m.group(1) if m else None


def _git(args: list[str], timeout: float, what: str) -> str:
    """Run git without a credential prompt and within ``timeout``; its stdout, or a SourceUpdateError with advice."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise SourceUpdateError(
            "git is not installed: it is needed to update an install made with `install.sh --from-git`"
        ) from None
    except subprocess.TimeoutExpired:
        raise SourceUpdateError(f"{what} timed out after {timeout:.0f} s: check the network and try again") from None
    if r.returncode == 0:
        return r.stdout
    err = (r.stderr or r.stdout or "").strip()
    last = err.splitlines()[-1][:140] if err else ""
    if any(hint in err.lower() for hint in _AUTH_HINTS):
        raise SourceUpdateError(
            f"{what}: git could not sign in ({last}).\nIf the repository is private, give git your credentials "
            "(`gh auth login && gh auth setup-git`), then run `k3code update` again."
        )
    if _OFFLINE_HINTS.search(err):
        raise SourceUpdateError(f"{what}: no network ({last}). Check the connection and try again.")
    raise SourceUpdateError(f"{what} failed:\n{err[:600]}")


def remote_head(url: str, ref: str) -> str:
    """The commit ``ref`` (a branch, then a tag, then a full ref name) points at on ``url``.

    An annotated tag resolves to the commit it tags (``^{}``), which is what the installer builds and names the
    version after. A full commit SHA is returned as is: it is pinned and needs no network."""
    if is_commit_sha(ref):
        return ref
    out = _git(
        ["ls-remote", url, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}", ref],
        LS_REMOTE_TIMEOUT,
        f"git ls-remote {url}",
    )
    refs: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if sha and name:
            refs.setdefault(name, sha)
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", ref):
        if name in refs:  # ls-remote also matches on a tail (`main` hits `refs/heads/feature/main`): not those
            return refs[name]
    raise SourceUpdateError(
        f"'{ref}' was not found on {url}: the branch or tag this install follows is gone. Reinstall from another "
        f"one: {installer_command(f'--from-git {url} --ref Main')}"
    )


#: The temporary clone update_from_git runs the ref's installer from, and the file in it naming the update that owns it
#: (its pid, then when that process started, as in the install lock).
STAGING_PREFIX = ".update-git-"
STAGING_OWNER = "owner"
#: How long a staging directory with no readable owner file is left alone: an update between mkdtemp and writing it.
STAGING_GRACE = 60.0


def _staging_dir_is_stale(d: Path) -> bool:
    """Whether the update that made staging directory ``d`` is gone (a pid now naming another process counts as gone).

    A directory without an owner file (made before the file existed, or by an update killed before writing it) is
    stale once it is older than STAGING_GRACE."""
    try:
        pid_line, _, started = (d / STAGING_OWNER).read_text().partition("\n")
        pid = int(pid_line)
    except (OSError, ValueError):
        try:
            return time.time() - d.stat().st_mtime > STAGING_GRACE
        except OSError:
            return False
    if not _pid_running(pid):
        return True
    started = started.strip()
    now = _process_start(pid) if started else ""
    return bool(now) and now != started


def _sweep_stale_staging_dirs() -> None:
    """Remove the ``.update-git-*`` directories that killed updates left in the data dir: SIGKILL skips the ``finally``
    that removes them, and nothing else ever did."""
    try:
        entries = list(data_dir().iterdir())
    except OSError:
        return
    for d in entries:
        if d.name.startswith(STAGING_PREFIX) and d.is_dir() and not d.is_symlink() and _staging_dir_is_stale(d):
            shutil.rmtree(d, ignore_errors=True)


def update_from_git(url: str, ref: str, *, progress: Callable[[str], None] = _show_progress) -> str:
    """Stage a new version from ``ref`` on ``url`` via that ref's own installer (``--no-activate``); its name.

    The installer's progress lines go to ``progress`` (stderr by default) while it builds.

    The installer comes from a shallow fetch of the ref into a temporary directory under the data dir (never
    ``versions/``, which prune and the version list read), removed again whatever happens (and by the next update
    when this one is killed outright). ``--from-git`` (not ``--from-source``) keeps the install a git install: it
    records ``.ref`` and no checkout path."""
    data_dir().mkdir(parents=True, exist_ok=True)
    _sweep_stale_staging_dirs()
    tmp = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=data_dir()))
    try:
        (tmp / STAGING_OWNER).write_text(f"{os.getpid()}\n{_process_start(os.getpid())}\n")
        src = tmp / "src"
        _git(["-c", "init.defaultBranch=main", "init", "-q", str(src)], LS_REMOTE_TIMEOUT, "git init")
        _git(["-C", str(src), "fetch", "-q", "--depth", "1", url, ref], FETCH_TIMEOUT, f"git fetch {ref} from {url}")
        _git(["-C", str(src), "checkout", "-q", "FETCH_HEAD"], LS_REMOTE_TIMEOUT, "git checkout")
        installer = src / "install" / "install.sh"
        if not installer.is_file():
            raise SourceUpdateError(f"'{ref}' on {url} has no install/install.sh: it is not a k3code repository")
        r = _run_installer(
            [
                "sh",
                str(installer),
                "--from-git",
                url,
                "--ref",
                ref,
                "--yes",
                "--no-setup",
                "--no-activate",
                "--print-version",
            ],
            stdin=subprocess.DEVNULL,
            env=_installer_env(K3CODE_DATA=str(data_dir()), GIT_TERMINAL_PROMPT="0"),
            progress=progress,
        )
        if r.returncode or not r.stdout.strip():
            tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-INSTALLER_TAIL_LINES:])
            raise SourceUpdateError(f"the installer failed (exit {r.returncode}):\n{tail}")
        return r.stdout.strip().splitlines()[-1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
