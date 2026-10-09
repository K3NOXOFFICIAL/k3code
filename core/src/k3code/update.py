"""Versioned installs under ``<data>/versions/<ver>`` with an atomic ``current`` symlink, update and rollback."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
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
    repo = s.get("repo", DEFAULT_REPO)
    return {
        "channel": s.get("channel", "stable"),
        "repo": repo,
        "source": s.get("source", ""),
        # the git remote a `install.sh --from-git` install updates from (a fork or mirror may set its own)
        "url": s.get("url") or f"https://github.com/{repo}.git",
    }


def _headers(token: str | None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_latest(channel: str = "stable", repo: str = DEFAULT_REPO, token: str | None = None) -> Release | None:
    """Newest release on ``channel`` (``stable`` skips prereleases; ``dev`` takes anything)."""
    r = httpx.get(f"https://api.github.com/repos/{repo}/releases?per_page=30", headers=_headers(token), timeout=15)
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
        return (
            "systemd-run is not available: run `k3code update --yes` from a terminal "
            "(the daemon cannot safely update the unit it is running in)."
        )
    unit = f"k3code-update-{int(time.time())}"
    r = subprocess.run(
        [runner, "--user", "--collect", "--quiet", f"--unit={unit}", exe, "update", "--yes"],
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


def git_pull_command(checkout: Path) -> list[str]:
    """The command that fast-forwards ``checkout``.

    A checkout on a Windows drive (``/mnt/c/...``) seen from WSL was cloned with Windows git, which holds the user's
    GitHub credentials. WSL's own git has none and cannot pull a private repository, so use ``git.exe`` there."""
    path = str(checkout)
    if _in_wsl() and re.match(r"^/mnt/[a-z]/", path) and (exe := shutil.which("git.exe")):
        win = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, check=False).stdout.strip()
        if win:
            return [exe, "-C", win, "pull", "--ff-only"]
    return ["git", "-C", path, "pull", "--ff-only"]


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


def update_from_source(checkout: Path, *, pull: bool = True) -> str:
    """``git pull --ff-only`` then stage a new version via the installer (``--no-activate``); returns its name."""
    if pull:
        pull_checkout(checkout)
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
        check=False,
        capture_output=True,
        text=True,
    )
    if r.returncode:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-8:])
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
        f"one: `sh install.sh --from-git {url} --ref Main`"
    )


def update_from_git(url: str, ref: str) -> str:
    """Stage a new version from ``ref`` on ``url`` via that ref's own installer (``--no-activate``); its name.

    The installer comes from a shallow fetch of the ref into a temporary directory under the data dir (never
    ``versions/``, which prune and the version list read), removed again whatever happens. ``--from-git`` (not
    ``--from-source``) keeps the install a git install: it records ``.ref`` and no checkout path."""
    data_dir().mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".update-git-", dir=data_dir()))
    try:
        src = tmp / "src"
        _git(["-c", "init.defaultBranch=main", "init", "-q", str(src)], LS_REMOTE_TIMEOUT, "git init")
        _git(["-C", str(src), "fetch", "-q", "--depth", "1", url, ref], FETCH_TIMEOUT, f"git fetch {ref} from {url}")
        _git(["-C", str(src), "checkout", "-q", "FETCH_HEAD"], LS_REMOTE_TIMEOUT, "git checkout")
        installer = src / "install" / "install.sh"
        if not installer.is_file():
            raise SourceUpdateError(f"'{ref}' on {url} has no install/install.sh: it is not a k3code repository")
        r = subprocess.run(
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
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "K3CODE_DATA": str(data_dir()), "GIT_TERMINAL_PROMPT": "0"},
        )
        if r.returncode or not r.stdout.strip():
            tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-8:])
            raise SourceUpdateError(f"the installer failed (exit {r.returncode}):\n{tail}")
        return r.stdout.strip().splitlines()[-1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
