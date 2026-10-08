"""Versioned installs under ``<data>/versions/<ver>`` with an atomic ``current`` symlink, update and rollback."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from k3code import service
from k3code.paths import data_dir, user_config_path

DEFAULT_REPO = "K3NOXOFFICIAL/k3code"
KEEP_VERSIONS = 3
DAEMON_WAIT_SECONDS = 120


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
    """Sortable key: numeric core, a pre-release (``-dev.3``) sorts before the release."""
    v = v.lstrip("v").split("+")[0]
    core, _, pre = v.partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.]", core))
    return (nums, 0 if pre else 1, pre)


# -- smoke test / daemon health -------------------------------------------------


def smoke_test(vdir: Path, timeout: float = 60.0) -> tuple[bool, str]:
    """``k3code --version`` and ``k3code doctor --json --no-probe`` (no fails) using the version's own venv."""
    exe = vdir / "venv" / "bin" / "k3code"
    if not exe.exists():
        return False, f"{exe} missing"
    env = {**os.environ, "K3CODE_DATA": str(data_dir())}
    try:
        v = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
        if v.returncode != 0:
            return False, f"--version failed: {v.stderr.strip()[:200]}"
        d = subprocess.run(
            [str(exe), "doctor", "--json", "--no-probe"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        try:
            summary = json.loads(d.stdout).get("summary", {})
        except ValueError:
            return False, f"doctor emitted no JSON (exit {d.returncode}): {d.stderr.strip()[:200]}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    if summary.get("fail", 0):
        return False, f"doctor reports {summary['fail']} failing check(s)"
    return True, v.stdout.strip()


def restart_daemon() -> None:
    if service.is_installed():
        service._systemctl("restart", service.UNIT_NAME)  # noqa: SLF001


def daemon_healthy() -> bool:
    return service.active_state() == "active"


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


def activate(
    version: str,
    *,
    restart: Callable[[], None] = restart_daemon,
    healthy: Callable[[], bool] = daemon_healthy,
    daemon_installed: Callable[[], bool] = service.is_installed,
    smoke: Callable[[Path], tuple[bool, str]] = smoke_test,
    wait: Callable[..., bool] = wait_healthy,
) -> UpdateResult:
    """Smoke-test ``versions/<version>``, switch to it, restart the daemon; roll back on any failure."""
    vdir = versions_dir() / version
    prev = current_version()
    ok, detail = smoke(vdir)
    if not ok:
        return UpdateResult(False, version, f"smoke test failed, not switching: {detail}", log=[detail])
    switch_to(version)
    log = [f"switched current -> {version} (was {prev})"]
    if daemon_installed():
        restart()
        if not wait(healthy):
            if prev:
                switch_to(prev)
                restart()
                log.append(f"daemon unhealthy after {DAEMON_WAIT_SECONDS}s; rolled back to {prev}")
                return UpdateResult(False, version, log[-1], rolled_back=True, log=log)
            return UpdateResult(False, version, "daemon unhealthy and no previous version to roll back to", log=log)
    return UpdateResult(True, version, f"updated to {version}", log=log)


def rollback(
    *, restart: Callable[[], None] = restart_daemon, daemon_installed: Callable[[], bool] = service.is_installed
) -> UpdateResult:
    prev = previous_version()
    cur = current_version()
    if not prev:
        return UpdateResult(False, cur or "", "no previous version to roll back to")
    switch_to(prev)  # records `cur` as the new previous, so rollback twice toggles
    if daemon_installed():
        restart()
    return UpdateResult(True, prev, f"rolled back {cur} -> {prev}", rolled_back=True)


def prune(keep: int = KEEP_VERSIONS) -> list[str]:
    """Delete the oldest version dirs beyond ``keep``, never the current or previous one."""
    protected = {current_version(), previous_version()}
    vers = sorted(installed_versions(), key=version_key, reverse=True)
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
    return {"channel": s.get("channel", "stable"), "repo": s.get("repo", DEFAULT_REPO), "source": s.get("source", "")}


def _headers(token: str | None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_latest(channel: str = "stable", repo: str = DEFAULT_REPO, token: str | None = None) -> Release | None:
    """Newest release on ``channel`` (``stable`` skips prereleases; ``dev`` takes anything)."""
    r = httpx.get(f"https://api.github.com/repos/{repo}/releases?per_page=30", headers=_headers(token), timeout=15)
    if r.status_code in (401, 403, 404):
        raise PermissionError(f"GitHub API {r.status_code}: the repo is private, set GITHUB_TOKEN (or `gh auth login`)")
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


def install_release(rel: Release, token: str | None, uv: str | None = None) -> Path:
    """Download + verify the release assets and build ``versions/<ver>`` (not yet activated).

    Never touches a version that is installed and complete, nor the current or previous one: ``/update now`` used
    to rmtree ``versions/<ver>`` even when it was the live install, deleting the running k3code.
    """
    uv = uv or shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    vdir = versions_dir() / rel.version
    if vdir.exists():
        if (vdir / ".complete").is_file():
            return vdir  # already installed: nothing to download or rebuild
        if rel.version in (current_version(), previous_version()):
            raise ValueError(f"refusing to rebuild {rel.version}: it is the active or the previous version")
        shutil.rmtree(vdir)  # an interrupted earlier download
    # Build in place: venvs are not relocatable (absolute shebangs). `.complete` is written last.
    vdir.mkdir(parents=True)
    try:
        dl = vdir / ".dl"
        dl.mkdir()
        files: dict[str, Path] = {}
        for name, url in rel.assets.items():
            files[name] = dl / name
            _download(url, files[name], token)
        sums = {}
        if "SHA256SUMS" in files:
            for line in files["SHA256SUMS"].read_text().splitlines():
                parts = line.split()
                if len(parts) == 2:
                    sums[parts[1].lstrip("*")] = parts[0]
        for name, path in files.items():
            if name != "SHA256SUMS" and sums.get(name) and sums[name] != _sha256(path):
                raise ValueError(f"checksum mismatch for {name}")
        wheel = next((p for n, p in files.items() if n.endswith(".whl")), None)
        if wheel is None:
            raise ValueError("release has no wheel")
        subprocess.run([uv, "venv", "--python", ">=3.12", str(vdir / "venv")], check=True, capture_output=True)
        py = str(vdir / "venv" / "bin" / "python")
        subprocess.run([uv, "pip", "install", "--python", py, str(wheel)], check=True, capture_output=True)
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


def apply_detached() -> str:
    """Run ``k3code update --yes`` outside this process's service unit, so the update can restart the unit.

    The daemon used to install, restart its own unit from a worker thread and then poll for health *inside* the unit
    it was restarting: systemd SIGTERMed the whole cgroup (the poller included), so the auto-rollback branch could
    never run and the shutdown hung on the executor thread. A transient ``systemd-run --user`` unit survives it.
    """
    exe = shutil.which("k3code") or str(Path(sys.argv[0]).resolve())
    runner = shutil.which("systemd-run")
    if runner is None:
        return ("systemd-run is not available: run `k3code update --yes` from a terminal "
                "(the daemon cannot safely update the unit it is running in).")
    unit = f"k3code-update-{int(time.time())}"
    r = subprocess.run(
        [runner, "--user", "--collect", "--quiet", f"--unit={unit}", exe, "update", "--yes"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if r.returncode != 0:
        return f"Could not start the update: {(r.stderr or r.stdout).strip()[:300]}"
    return (f"Update started in the background ({unit}). The daemon restarts when it is installed and smoke-tested; "
            f"it is rolled back automatically if the new version does not come up. Follow it with "
            f"`journalctl --user -u {unit}`.")


def source_checkout() -> Path | None:
    s = update_settings()["source"]
    if s:
        return Path(s).expanduser()
    f = data_dir() / "source_path"
    return Path(f.read_text().strip()) if f.is_file() else None


def update_from_source(checkout: Path) -> str:
    """``git pull --ff-only`` then stage a new version via the installer (``--no-activate``); returns its name."""
    subprocess.run(["git", "-C", str(checkout), "pull", "--ff-only"], check=True)
    r = subprocess.run(
        [
            "sh",
            str(checkout / "install" / "install.sh"),
            "--from-source",
            "--yes",
            "--no-setup",
            "--no-activate",
            "--print-version",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip().splitlines()[-1]
