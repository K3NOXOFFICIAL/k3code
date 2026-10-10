from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "install" / "install.sh"
UNINSTALL = REPO / "install" / "uninstall.sh"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="installer needs uv present")
# bubblewrap is the Linux sandbox: the installer checks for it only on Linux
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="bubblewrap check runs on Linux only")


def run(
    home: Path,
    script: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    drop: tuple[str, ...] = (),
    path_front: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),  # never read a real ~/.config/k3code/env
        "K3_STUB_VENV": "1",  # fake core venv: no pip / network
        "K3_SKIP_TUI": "1",
        "K3_SKIP_GO": "1",
        "K3_NO_DOWNLOAD": "1",
        "K3_NO_GH": "1",
    }
    for k in drop:
        env.pop(k, None)
    env.update(env_extra or {})
    if path_front is not None:
        env["PATH"] = f"{path_front}{os.pathsep}{env['PATH']}"
    # A new session has no controlling terminal, so the installer cannot open /dev/tty and prompt.
    return subprocess.run(
        ["sh", str(script), *args], env=env, capture_output=True, text=True, check=False, start_new_session=True
    )


def stub_bin(root: Path, name: str, body: str) -> Path:
    """A directory holding one executable stub (a shell script) named ``name``; put it first on PATH."""
    d = root / "stubbin"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return d


def snapshot(home: Path) -> dict[str, tuple[float, str]]:
    out = {}
    for p in sorted(home.rglob("*")):
        if ".k3code" in p.parts or p.name == "install.log":
            continue  # doctor may touch its own home dir; the install log grows on every run by design
        # every run takes and drops its lock directory in the install root, which touches that directory's mtime
        lock_parent = p.name == "k3code" and p.parent.name == "share"
        out[str(p.relative_to(home))] = (
            0 if lock_parent else p.lstat().st_mtime_ns,
            os.readlink(p) if p.is_symlink() else "",
        )
    return out


def test_install_layout_and_idempotent(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--from-source", "--yes", "--no-setup")
    assert r.returncode == 0, r.stderr
    data = tmp_path / ".local" / "share" / "k3code"
    (ver,) = [p.name for p in (data / "versions").iterdir()]
    assert ver.startswith((REPO / "VERSION").read_text().strip())
    assert (data / "current").is_symlink() and (data / "current").resolve().name == ver
    assert (tmp_path / ".local" / "bin" / "k3code").is_symlink()
    assert (data / "versions" / ver / ".complete").is_file()
    assert (data / "source_path").read_text().strip() == str(REPO)
    before = snapshot(tmp_path)
    r2 = run(tmp_path, INSTALL, "--from-source", "--yes", "--no-setup")
    assert r2.returncode == 0, r2.stderr
    assert "already installed" in r2.stderr
    assert snapshot(tmp_path) == before  # second run changes nothing


def test_from_git_fetch_failure_leaves_no_install(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-repo.git"  # a local path that is not a repository: fetch fails offline
    r = run(tmp_path, INSTALL, "--yes", "--from-git", f"file://{missing}", "--ref", "Main")
    assert r.returncode != 0
    assert "could not fetch 'Main'" in r.stderr
    assert not (tmp_path / ".local" / "share" / "k3code" / "versions").exists()
    assert not (tmp_path / ".local" / "bin" / "k3code").exists()


def test_unknown_option_is_rejected(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--bogus")
    assert r.returncode != 0 and "unknown option" in r.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_installed_requirements_are_the_locked_runtime_set() -> None:
    # install_core_copy installs `uv export --locked --no-dev` with --require-hashes; the stubbed installer tests
    # never run that path.
    src = INSTALL.read_text()
    assert "--no-hashes" not in src and '--require-hashes -r "$REQS"' in src
    uv = shutil.which("uv") or "uv"
    core = str(REPO / "core")
    lock = subprocess.run([uv, "lock", "--check", "--offline", "--project", core], capture_output=True)
    assert lock.returncode == 0, lock.stderr
    export = subprocess.run(
        [uv, "export", "--project", core, "--locked", "--no-dev", "--no-emit-project"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in export.stdout.splitlines() if "==" in ln and not ln.startswith(" ")]
    names = {ln.split("==")[0].strip().lower() for ln in lines}
    assert {"mcp", "pydantic", "click", "pyyaml", "prompt-toolkit"} <= names
    assert not names & {"pytest", "pytest-asyncio", "ruff", "respx", "pexpect"}
    assert export.stdout.count("--hash=sha256:") >= len(lines)  # every pinned package carries its hash


def test_uninstall_keeps_user_data_unless_purge(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--yes", "--no-setup").returncode == 0
    (tmp_path / ".k3code").mkdir(exist_ok=True)
    (tmp_path / ".k3code" / "config.yaml").write_text("x: 1\n")
    r = run(tmp_path, UNINSTALL)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".local" / "share" / "k3code").exists()
    assert not (tmp_path / ".local" / "bin" / "k3code").exists()
    assert (tmp_path / ".k3code" / "config.yaml").is_file()
    assert run(tmp_path, UNINSTALL, "--purge", "--yes").returncode == 0
    assert not (tmp_path / ".k3code").exists()


def test_windows_shell_points_to_install_ps1(tmp_path: Path) -> None:
    # Git Bash / MSYS / Cygwin: install.sh cannot install there and says to use install.ps1 (WSL) instead.
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uname").write_text('#!/bin/sh\n[ "$1" = -s ] && echo MINGW64_NT-10.0-19045 || echo x86_64\n')
    (fake / "uname").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    r = subprocess.run(["sh", str(INSTALL), "--check"], env=env, capture_output=True, text=True, check=False)
    assert r.returncode != 0
    assert "install.ps1" in r.stderr


FAKE_WSL = """#!/bin/sh
# wsl.exe stand-in for Windows 10's inbox WSL: Ubuntu is the one to use (FAKE_WSL_DOCKER=1: Docker Desktop's distro is
# the default), there is no --cd option, and --exec runs commands here (wslpath -a returns the path: it is a Linux
# one here already)
if [ "$1" = --list ]; then
  printf '  NAME              STATE           VERSION\\n'
  if [ "${FAKE_WSL_DOCKER:-0}" = 1 ]; then
    printf '* docker-desktop    Running         2\\n  Ubuntu            Stopped         2\\n'
  else printf '* Ubuntu            Running         2\\n'; fi
  exit 0
fi
while [ $# -gt 0 ]; do
  case "$1" in
    -d) [ "$2" = Ubuntu ] || { echo "not the distro to install into: $2" >&2; exit 8; }; shift ;;
    --cd) echo "Invalid command line option: --cd" >&2; exit 1 ;;
    --exec) shift; if [ "$1" = wslpath ]; then printf '%s\\n' "$3"; exit 0; fi; exec "$@" ;;
  esac
  shift
done
"""


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell (pwsh)")
@pytest.mark.parametrize("docker_default", [False, True], ids=["ubuntu-default", "docker-desktop-default"])
def test_install_ps1_runs_install_sh_in_wsl_and_writes_shims(tmp_path: Path, docker_default: bool) -> None:
    wsl = tmp_path / "wsl"
    wsl.write_text(FAKE_WSL)
    wsl.chmod(0o755)
    wslpath = stub_bin(tmp_path, "wslpath", 'printf "%s\\n" "$2"\n')  # inside "WSL" (here) paths are Linux already
    appdata = tmp_path / "appdata"
    env = {
        "PATH": f"{wslpath}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "LOCALAPPDATA": str(appdata),
        "K3_WSL": str(wsl),
        "FAKE_WSL_DOCKER": "1" if docker_default else "0",
        "K3_STUB_VENV": "1",
        "K3_SKIP_TUI": "1",
        "K3_SKIP_GO": "1",
        "K3_NO_DOWNLOAD": "1",
    }

    def ps(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["pwsh", "-NoProfile", "-File", str(REPO / "install" / script), *args]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)

    r = ps("install.ps1", "-NoModifyPath", "--from-source", "--yes")
    assert r.returncode == 0, r.stderr
    assert "WSL distribution: Ubuntu" in r.stderr
    assert (tmp_path / ".local" / "share" / "k3code" / "current").is_symlink()
    shim = (appdata / "k3code" / "bin" / "k3code.cmd").read_text()
    assert f'wsl.exe -d Ubuntu --exec sh -lc "exec {tmp_path}/.local/bin/k3code \\"$@\\"" k3code %*' in shim
    assert "--cd" not in shim  # wsl.exe starts in the current directory by itself; Windows 10's WSL has no --cd
    assert not (appdata / "k3code" / "bin" / "k3.cmd").exists()  # no k3 binary was built

    r = ps("uninstall.ps1")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".local" / "share" / "k3code").exists()
    assert not (appdata / "k3code").exists()


def test_relative_prefix_gives_absolute_links(tmp_path: Path) -> None:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "K3_STUB_VENV": "1", "K3_SKIP_TUI": "1"}
    env |= {"K3_SKIP_GO": "1", "K3_NO_DOWNLOAD": "1"}
    cmd = ["sh", str(INSTALL), "--from-source", "--prefix", "rel"]
    r = subprocess.run(cmd, env=env, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    link = tmp_path / "rel" / "bin" / "k3code"
    assert os.readlink(link).startswith(str(tmp_path / "rel")), os.readlink(link)
    assert link.resolve().is_file()


def mini_checkout(tmp_path: Path) -> tuple[Path, list[str]]:
    """A committed checkout holding just what install.sh --from-source needs; and the git command for it."""
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "install").mkdir()
    (src / "core" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (src / "VERSION").write_text("9.9.9\n")
    shutil.copy(INSTALL, src / "install" / "install.sh")
    git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return src, git


def test_a_checkout_owned_by_someone_else_still_names_its_commit(tmp_path: Path) -> None:
    # A Windows clone seen from WSL, or a shared checkout, can belong to another user: git calls it "dubious" and
    # refuses every command. The version was then X.Y.Z-src for every commit, so `k3code update --from-source`
    # reported an update and kept running the first build. GIT_TEST_ASSUME_DIFFERENT_OWNER makes git see that.
    src, git = mini_checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    foreign = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    assert subprocess.run(["git", "-C", str(src), "status"], env={**os.environ, **foreign}, check=False).returncode

    def version() -> str:
        r = run(home, src / "install" / "install.sh", "--from-source", "--print-version", env_extra=foreign)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    first = version()
    head = subprocess.run([*git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True)
    assert first == f"9.9.9-src.{head.stdout.strip()}"
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "next"], check=True)
    assert version() != first


def test_a_checkout_git_cannot_read_is_refused_not_misnamed(tmp_path: Path) -> None:
    # a .git that git cannot read (here: no commit yet) would name the build X.Y.Z-src, the same for every state
    src, _ = mini_checkout(tmp_path)
    shutil.rmtree(src / ".git")
    subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
    r = run(tmp_path / "home", src / "install" / "install.sh", "--from-source", "--print-version")
    assert r.returncode != 0 and "git cannot read the checkout" in r.stderr
    assert not r.stdout.strip()


def test_from_source_uncommitted_edits_get_their_own_version(tmp_path: Path) -> None:
    src, _ = mini_checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    def version() -> str:
        r = run(home, src / "install" / "install.sh", "--from-source", "--print-version")
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    clean = version()
    assert ".dirty" not in clean
    (src / "core" / "pyproject.toml").write_text("[project]\nname = 'y'\n")
    dirty = version()
    assert dirty.startswith(clean + ".dirty")
    (src / "core" / "new.py").write_text("x = 1\n")  # an untracked file changes it again
    assert version() not in (clean, dirty)


def test_without_git_each_checkout_gets_its_own_version_from_its_files(tmp_path: Path) -> None:
    # Without git every --from-source build was named X.Y.Z-src, so a second checkout counted as installed already.
    # The macOS /usr/bin/git stub: present on PATH, but fails without the developer tools.
    nogit = stub_bin(tmp_path, "git", 'echo "xcode-select: note: no developer tools were found" >&2\nexit 1\n')
    home = tmp_path / "home"
    home.mkdir()

    def checkout(name: str, extra: str) -> Path:
        src = tmp_path / name
        (src / "core").mkdir(parents=True)
        (src / "install").mkdir()
        (src / "core" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (src / "core" / "extra.py").write_text(extra)
        (src / "VERSION").write_text("9.9.9\n")
        shutil.copy(INSTALL, src / "install" / "install.sh")
        return src

    def install(src: Path) -> subprocess.CompletedProcess[str]:
        r = run(home, src / "install" / "install.sh", "--from-source", "--minimal", "--print-version", path_front=nogit)
        assert r.returncode == 0, r.stderr
        return r

    a, b = checkout("a", "x = 1\n"), checkout("b", "x = 2\n")
    first = install(a)
    ver_a = first.stdout.strip()
    assert re.fullmatch(r"9\.9\.9-src\.tree[0-9a-f]{12}", ver_a), ver_a
    ver_b = install(b).stdout.strip()
    assert ver_b != ver_a  # another checkout is another version, built next to the first
    assert {p.name for p in (home / DATA_REL / "versions").iterdir()} == {ver_a, ver_b}

    # the same tree keeps its name: file times, caches and build output do not count
    for p in a.rglob("*"):
        os.utime(p, (1_000_000, 1_000_000))
    (a / "core" / "__pycache__").mkdir()
    (a / "core" / "__pycache__" / "extra.cpython-312.pyc").write_bytes(b"\0")
    (a / "tui" / "node_modules").mkdir(parents=True)
    (a / "tui" / "node_modules" / "dep.js").write_text("1\n")
    # files the install never reads (Finder, editors, agents) do not count either
    (a / ".DS_Store").write_bytes(b"\0")
    (a / "core" / ".DS_Store").write_bytes(b"\0")
    (a / ".idea").mkdir()
    (a / ".idea" / "workspace.xml").write_text("<x/>\n")
    again = install(a)
    assert again.stdout.strip() == ver_a
    assert "already installed" in again.stderr


def test_uninstall_removes_the_unit_under_xdg_config_home(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    unit = xdg / "systemd" / "user" / "k3code.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    # the uninstaller's fallback calls systemctl --user: a stub, never the real user manager
    calls = tmp_path / "systemctl.log"
    stubs = stub_bin(tmp_path, "systemctl", f'echo "systemctl $*" >>"{calls}"\nexit 0\n')
    env = {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(xdg)}
    r = subprocess.run(["sh", str(UNINSTALL)], env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert not unit.exists()  # removed even though no k3code is installed to do it
    assert "systemctl --user disable --now k3code.service" in calls.read_text()


def test_presetup_is_the_default_and_minimal_skips_it(tmp_path: Path) -> None:
    log = tmp_path / "curl.log"
    curl = stub_bin(tmp_path, "curl", f'echo "curl $*" >>"{log}"\nexit 1\n')
    default_home = tmp_path / "default"
    default_home.mkdir()
    r = run(default_home, INSTALL, "--from-source", "--yes", path_front=curl)
    assert r.returncode == 0, r.stderr
    assert "presetup (optional" in r.stderr
    assert not log.exists()  # under stubs presetup makes no network call

    minimal_home = tmp_path / "minimal"
    minimal_home.mkdir()
    m = run(minimal_home, INSTALL, "--from-source", "--yes", "--minimal", path_front=curl)
    assert m.returncode == 0, m.stderr
    assert "presetup (optional" not in m.stderr
    assert not log.exists()
    assert "Installed k3code" in m.stderr


def test_unreachable_private_repo_falls_back_to_this_checkout(tmp_path: Path) -> None:
    # A Windows clone made with Windows git, installed through install.ps1: the WSL side has no GitHub credentials, so
    # finding the latest version fails. With nothing installed yet, the installer builds the checkout it runs from.
    real_git = shutil.which("git")
    stubs = stub_bin(
        tmp_path,
        "git",
        'if [ "$1" = ls-remote ]; then\n'
        "  echo \"fatal: could not read Username for 'https://github.com'\" >&2; exit 128\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n',
    )
    r = run(tmp_path, INSTALL, "--yes", "--minimal", path_front=stubs)
    assert r.returncode == 0, r.stderr
    assert "installing this checkout instead (--from-source)" in r.stderr
    assert "Installed k3code" in r.stderr


def test_update_flags_print_only_the_version(tmp_path: Path) -> None:
    # k3code update runs the installer with these flags and reads the version from stdout: nothing else may land there.
    r = run(tmp_path, INSTALL, "--from-source", "--yes", "--no-setup", "--no-activate", "--print-version")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith((REPO / "VERSION").read_text().strip())
    assert "presetup" not in r.stderr  # presetup runs only after activation


def test_presetup_second_run_changes_nothing(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--yes").returncode == 0
    before = snapshot(tmp_path)
    again = run(tmp_path, INSTALL, "--from-source", "--yes")
    assert again.returncode == 0, again.stderr
    assert "presetup (optional" in again.stderr  # the phase still runs and reports
    assert snapshot(tmp_path) == before


@linux_only
def test_bwrap_missing_prints_the_command_and_exits_zero(tmp_path: Path) -> None:
    sudo_log = tmp_path / "sudo.log"
    sudo = stub_bin(tmp_path, "sudo", f'echo "sudo $*" >>"{sudo_log}"\nexit 1\n')
    r = run(tmp_path, INSTALL, "--from-source", "--yes", env_extra={"K3_BWRAP": "/nonexistent/bwrap"}, path_front=sudo)
    assert r.returncode == 0, r.stderr
    assert "bubblewrap is not installed" in r.stderr
    assert "install it:" in r.stderr
    assert not sudo_log.exists()  # never sudo unattended, not even when the command is known


@linux_only
def test_bwrap_installed_but_unusable_is_a_warning(tmp_path: Path) -> None:
    bwrap = stub_bin(tmp_path, "bwrap", "exit 1\n")
    r = run(tmp_path, INSTALL, "--from-source", "--yes", env_extra={"K3_BWRAP": str(bwrap / "bwrap")})
    assert r.returncode == 0, r.stderr
    assert "installed but cannot create a sandbox" in r.stderr


@linux_only
def test_bwrap_usable_is_reported_ok(tmp_path: Path) -> None:
    bwrap = stub_bin(tmp_path, "bwrap", "exit 0\n")
    r = run(tmp_path, INSTALL, "--from-source", "--yes", env_extra={"K3_BWRAP": str(bwrap / "bwrap")})
    assert r.returncode == 0, r.stderr
    assert "sandbox ok" in r.stderr


def _source_repo(root: Path) -> Path:
    """A minimal k3code-shaped git repository (VERSION, core/pyproject.toml) with a tag v0.0.1 and branch main."""
    src = root / "srcrepo"
    (src / "core").mkdir(parents=True)
    (src / "VERSION").write_text("0.0.1\n")
    (src / "core" / "pyproject.toml").write_text("[project]\nname = 'k3code'\nversion = '0.0.1'\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(src)]
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(src)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    subprocess.run([*git, "tag", "v0.0.1"], check=True)
    return src


def test_from_git_tag_reruns_without_network(tmp_path: Path) -> None:
    src = _source_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    first = run(home, INSTALL, "--yes", "--from-git", f"file://{src}", "--ref", "v0.0.1")
    assert first.returncode == 0, first.stderr
    # the remote is gone now: a complete tag install must not touch it
    gone = f"file://{tmp_path / 'gone.git'}"
    again = run(home, INSTALL, "--yes", "--from-git", gone, "--ref", "v0.0.1")
    assert again.returncode == 0, again.stderr
    assert "already installed" in again.stderr
    assert "fetching" not in again.stderr


def test_from_git_branch_falls_back_to_the_installed_build_when_offline(tmp_path: Path) -> None:
    src = _source_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    assert run(home, INSTALL, "--yes", "--from-git", f"file://{src}", "--ref", "main").returncode == 0
    gone = f"file://{tmp_path / 'gone.git'}"
    again = run(home, INSTALL, "--yes", "--from-git", gone, "--ref", "main")
    assert again.returncode == 0, again.stderr
    assert "could not fetch 'main'" in again.stderr
    assert "Using the installed version" in again.stderr
    assert (home / ".local" / "bin" / "k3code").is_symlink()


def test_fetch_errors_separate_no_network_from_private_repo(tmp_path: Path) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    offline = "fatal: unable to access 'https://example.invalid/x.git/': Could not resolve host: example.invalid"
    private = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
    cases = {offline: "no network", private: "private or needs credentials"}
    for i, (text, phrase) in enumerate(cases.items()):
        body = f'case "$*" in *fetch*) echo "{text}" >&2; exit 128 ;; esac\nexec {real_git} "$@"\n'
        stubs = stub_bin(tmp_path / f"case{i}", "git", body)
        home = tmp_path / f"home{i}"
        home.mkdir()
        r = run(
            home, INSTALL, "--yes", "--from-git", "https://example.invalid/x.git", "--ref", "main", path_front=stubs
        )
        assert r.returncode != 0
        assert "could not fetch 'main'" in r.stderr
        assert phrase in r.stderr, r.stderr


def test_uv_temp_file_is_removed_when_the_uv_download_fails(tmp_path: Path) -> None:
    # uv is only found in ~/.local/bin here: drop that PATH entry so the installer tries to fetch uv itself
    path = os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / "uv").exists())
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    curl = stub_bin(tmp_path, "curl", "exit 22\n")
    home = tmp_path / "home"
    home.mkdir()
    r = run(
        home,
        INSTALL,
        "--from-source",
        "--yes",
        env_extra={"PATH": path, "TMPDIR": str(tmpdir)},
        drop=("K3_NO_DOWNLOAD",),  # that flag means --no-install-deps, which never downloads uv
        path_front=curl,
    )
    assert r.returncode != 0
    assert "could not download uv" in r.stderr
    assert list(tmpdir.glob("k3code-uv.*")) == []


def test_check_reports_the_node_floor_of_20(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--check")
    assert r.returncode == 0, r.stderr
    assert "node 20+ with npm" in r.stderr
    assert "node 18" not in r.stderr


def test_uninstall_removes_presetup_leftovers_and_keeps_user_data(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--yes").returncode == 0
    data = tmp_path / ".local" / "share" / "k3code"
    (data / "presetup").mkdir(exist_ok=True)
    (data / "presetup" / "chromium-marker").write_text("ok\n")
    (data / "browsers" / "chromium-1").mkdir(parents=True, exist_ok=True)
    (data / "browsers" / "chromium-1" / "chrome").write_text("binary\n")
    (tmp_path / ".k3code").mkdir(exist_ok=True)
    (tmp_path / ".k3code" / "config.yaml").write_text("x: 1\n")
    (tmp_path / ".config" / "k3code").mkdir(parents=True)
    (tmp_path / ".config" / "k3code" / "env").write_text("EXAMPLE_NAME=placeholder\n")

    r = run(tmp_path, UNINSTALL)
    assert r.returncode == 0, r.stderr
    assert not data.exists()  # presetup markers and the browser location go with the install
    assert (tmp_path / ".k3code" / "config.yaml").is_file()
    assert (tmp_path / ".config" / "k3code" / "env").is_file()
    assert run(tmp_path, UNINSTALL, "--purge", "--yes").returncode == 0
    assert not (tmp_path / ".k3code").exists()
    assert not (tmp_path / ".config" / "k3code").exists()


def test_presetup_shows_the_doctor_subset_and_ignores_its_status(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--from-source", "--yes")
    assert r.returncode == 0, r.stderr  # the stub doctor exits 1; the install still succeeds
    assert "health subset" in r.stderr
    assert "browser: stub" in r.stderr


CHROMIUM_MARKER = Path("presetup") / "chromium-1.63.0"


def test_chromium_is_on_by_default_at_a_version_independent_location(tmp_path: Path) -> None:
    r = run(tmp_path, INSTALL, "--from-source", "--yes")
    assert r.returncode == 0, r.stderr
    data = tmp_path / ".local" / "share" / "k3code"
    assert (data / CHROMIUM_MARKER).is_file()
    assert "Chromium stub" in r.stderr
    # outside versions/: an update prunes old versions but keeps the browser
    assert not any(p.name == "chromium-1.63.0" for p in (data / "versions").rglob("*"))


def test_chromium_opt_outs_write_no_marker(tmp_path: Path) -> None:
    skipped = tmp_path / "skip"
    skipped.mkdir()
    r = run(skipped, INSTALL, "--from-source", "--yes", env_extra={"K3CODE_SKIP_CHROMIUM": "1"})
    assert r.returncode == 0, r.stderr
    assert "Chromium skipped" in r.stderr
    assert not (skipped / ".local" / "share" / "k3code" / CHROMIUM_MARKER).exists()

    minimal = tmp_path / "minimal"
    minimal.mkdir()
    m = run(minimal, INSTALL, "--from-source", "--yes", "--minimal")
    assert m.returncode == 0, m.stderr
    assert not (minimal / ".local" / "share" / "k3code" / CHROMIUM_MARKER).exists()


def test_uninstall_removes_the_chromium_location(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--yes").returncode == 0
    data = tmp_path / ".local" / "share" / "k3code"
    assert (data / CHROMIUM_MARKER).is_file()
    assert run(tmp_path, UNINSTALL).returncode == 0
    assert not data.exists()


def test_presetup_doctor_sees_a_custom_prefix_data_dir(tmp_path: Path) -> None:
    # with --prefix the browser location is under that prefix, so the doctor must be told the data dir explicitly
    prefix = tmp_path / "custom"
    r = run(tmp_path, INSTALL, "--from-source", "--yes", "--prefix", str(prefix))
    assert r.returncode == 0, r.stderr
    assert f"data={prefix / 'share' / 'k3code'}" in r.stderr


def test_unit_files_share_one_restart_budget_and_a_recovery_unit(tmp_path, monkeypatch):
    """P1-2: the repo copies, the rendered unit and the recovery unit agree; systemd gives up one start after the
    daemon's own storm guard (safe mode), and the recovery unit starts the daemon again after a cooldown."""
    from k3code import daemon, service

    main_repo = (REPO / "install" / "systemd" / "k3code.service").read_text()
    recover_repo = (REPO / "install" / "systemd" / "k3code-recover.service").read_text()
    policy = ("StartLimitIntervalSec=", "StartLimitBurst=", "Restart=", "RestartSec=", "OnFailure=")

    def lines(text: str) -> list[str]:
        return [ln for ln in text.splitlines() if ln.startswith(policy)]

    assert lines(main_repo) == lines(service.render_unit("/usr/bin/k3code daemon"))
    assert service.START_LIMIT_BURST == daemon.RESTART_LIMIT + 1
    assert service.START_LIMIT_INTERVAL_S == int(daemon.RESTART_WINDOW_S)  # noqa: SIM300
    assert "OnFailure=k3code-recover.service" in main_repo
    assert recover_repo == service.render_recover_unit()
    assert "reset-failed k3code.service" in recover_repo and "systemctl --user start k3code.service" in recover_repo

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(service, "_systemctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))  # no real manager
    service.install()
    recover = tmp_path / "cfg" / "systemd" / "user" / "k3code-recover.service"
    assert recover.read_text() == service.render_recover_unit()
    service.uninstall()
    assert not recover.exists()


def test_passwordless_sudo_is_never_used_for_bubblewrap(tmp_path: Path) -> None:
    # a passwordless sudo is not consent: no sudo call at all without a person saying yes at a terminal
    sudo_log = tmp_path / "sudo.log"
    sudo = stub_bin(tmp_path, "sudo", f'echo "sudo $*" >>"{sudo_log}"\nexit 0\n')
    r = run(tmp_path, INSTALL, "--from-source", "--yes", env_extra={"K3_BWRAP": "/nonexistent/bwrap"}, path_front=sudo)
    assert r.returncode == 0, r.stderr
    assert not sudo_log.exists()


DATA_REL = Path(".local") / "share" / "k3code"


def test_root_is_refused_unless_allow_root(tmp_path: Path) -> None:
    ids = stub_bin(tmp_path, "id", 'case "$1" in -u) echo 0 ;; -un) echo root ;; *) echo "uid=0(root)" ;; esac\n')
    home = tmp_path / "home"
    home.mkdir()
    r = run(home, INSTALL, "--from-source", "--minimal", path_front=ids)
    assert r.returncode != 0
    assert "refusing to run as root" in r.stderr
    assert not (home / ".local").exists()  # refused before anything was written
    ok = run(home, INSTALL, "--from-source", "--minimal", "--allow-root", path_front=ids)
    assert ok.returncode == 0, ok.stderr
    assert (home / DATA_REL / "current").is_symlink()


def test_sudo_with_someone_elses_home_is_refused(tmp_path: Path) -> None:
    # `sudo -u bob` keeping alice's HOME: the install would land in a home that does not belong to the running user
    r = run(tmp_path, INSTALL, "--from-source", "--minimal", env_extra={"SUDO_USER": "alice"})
    assert r.returncode != 0
    assert "through sudo" in r.stderr
    assert not (tmp_path / DATA_REL).exists()


def test_a_held_install_lock_stops_a_second_install(tmp_path: Path) -> None:
    lock = tmp_path / DATA_REL / ".install.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")  # a live process holds it
    r = run(tmp_path, INSTALL, "--from-source", "--minimal")
    assert r.returncode != 0
    assert "another install" in r.stderr and str(os.getpid()) in r.stderr
    assert not (tmp_path / DATA_REL / "versions").exists()
    assert lock.is_dir()  # someone else's lock is never removed


def test_a_lock_left_by_a_killed_install_is_taken_over(tmp_path: Path) -> None:
    # SIGKILL mid-install leaves the lock (with its pid) and a version without .complete: the next run goes on
    data = tmp_path / DATA_REL
    lock = data / ".install.lock"
    lock.mkdir(parents=True)
    dead = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True, check=True).stdout.strip()
    (lock / "pid").write_text(f"{dead}\n")
    half = data / "versions" / "0.0.0-half"
    (half / "venv").mkdir(parents=True)
    done = data / "versions" / "0.0.0-done"
    done.mkdir()
    (done / ".complete").write_text("0.0.0-done\n")
    (data / "current").symlink_to(done)
    r = run(tmp_path, INSTALL, "--from-source", "--minimal")
    assert r.returncode == 0, r.stderr
    assert "taking over the install lock" in r.stderr and f"pid {dead}" in r.stderr
    assert not half.exists() and "unfinished version 0.0.0-half" in r.stderr
    assert (done / ".complete").is_file()  # the previous version stays (rollback)
    assert os.readlink(data / "current") != str(done)
    assert (data / "current").is_symlink() and not lock.exists()


def test_a_lock_without_a_pid_is_taken_over_only_when_old(tmp_path: Path) -> None:
    lock = tmp_path / DATA_REL / ".install.lock"
    lock.mkdir(parents=True)  # an install killed between mkdir and writing its pid
    young = run(tmp_path, INSTALL, "--from-source", "--minimal")
    assert young.returncode != 0
    assert "without a pid" in young.stderr and "rm -r" in young.stderr
    old = time.time() - 7 * 3600
    os.utime(lock, (old, old))
    r = run(tmp_path, INSTALL, "--from-source", "--minimal")
    assert r.returncode == 0, r.stderr
    assert "taking over the install lock" in r.stderr and "older than 6 hours" in r.stderr


def _lock_of(tmp_path: Path, pid: int, start: str, tui_tmp: str) -> Path:
    """The lock a killed install left: pid, start time, the TUI build dir it recorded (made under tmp_path/tmp)."""
    lock = tmp_path / DATA_REL / ".install.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{pid}\n")
    (lock / "start").write_text(f"{start}\n")
    (tmp_path / tui_tmp).mkdir(parents=True)
    (lock / "tui_tmp").write_text(f"{tmp_path / tui_tmp}\n")
    return lock


def test_a_lock_whose_pid_now_names_another_process_is_taken_over(tmp_path: Path) -> None:
    # the killed install's pid went to an unrelated live process (here pytest): its start time is not the lock's
    lock = _lock_of(tmp_path, os.getpid(), "0", "tmp/k3code-tui.Ab12Cd")
    r = run(tmp_path, INSTALL, "--from-source", "--minimal", env_extra={"TMPDIR": str(tmp_path / "tmp")})
    assert r.returncode == 0, r.stderr
    assert "taking over the install lock" in r.stderr and f"pid {os.getpid()} now belongs to another" in r.stderr
    assert not (tmp_path / "tmp" / "k3code-tui.Ab12Cd").exists() and "removed the TUI build directory" in r.stderr
    assert not lock.exists()


@pytest.mark.parametrize("recorded", ["tmp/not-k3code", "tmp/sub/k3code-tui.Zz99", "elsewhere/k3code-tui.Zz99"])
def test_a_recorded_path_that_is_not_a_tui_build_dir_is_left_alone(tmp_path: Path, recorded: str) -> None:
    dead = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True, check=True).stdout.strip()
    _lock_of(tmp_path, int(dead), "", recorded)
    r = run(tmp_path, INSTALL, "--from-source", "--minimal", env_extra={"TMPDIR": str(tmp_path / "tmp")})
    assert r.returncode == 0, r.stderr
    assert "taking over the install lock" in r.stderr and "removed the TUI build directory" not in r.stderr
    assert (tmp_path / recorded).is_dir()


def test_a_tui_build_dir_is_removed_when_tmpdir_ends_in_a_slash(tmp_path: Path) -> None:
    # macOS TMPDIR ends in "/", so mktemp recorded "<tmp>//k3code-tui.X" and the strict prefix check refused it
    dead = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True, check=True).stdout.strip()
    lock = _lock_of(tmp_path, int(dead), "", "tmp/k3code-tui.Ab12Cd")
    (lock / "tui_tmp").write_text(f"{tmp_path / 'tmp'}//k3code-tui.Ab12Cd\n")
    r = run(tmp_path, INSTALL, "--from-source", "--minimal", env_extra={"TMPDIR": f"{tmp_path / 'tmp'}/"})
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "tmp" / "k3code-tui.Ab12Cd").exists() and "removed the TUI build directory" in r.stderr


def test_a_lock_k3code_update_holds_stops_the_installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # one lock format for both: install.sh reads the pid and start time update.py wrote, and still refuses
    from k3code import update as upd

    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / DATA_REL))
    with upd.install_lock():
        (tmp_path / "tmp" / "k3code-tui.Ab12Cd").mkdir(parents=True)
        (tmp_path / DATA_REL / ".install.lock" / "tui_tmp").write_text(f"{tmp_path / 'tmp' / 'k3code-tui.Ab12Cd'}\n")
        r = run(tmp_path, INSTALL, "--from-source", "--minimal", env_extra={"TMPDIR": str(tmp_path / "tmp")})
        assert (tmp_path / DATA_REL / ".install.lock" / "pid").is_file()
    assert r.returncode != 0
    assert "another install" in r.stderr and str(os.getpid()) in r.stderr
    assert (tmp_path / "tmp" / "k3code-tui.Ab12Cd").is_dir()


@linux_only
def test_a_command_name_with_spaces_and_parentheses_keeps_its_lock(tmp_path: Path) -> None:
    # /proc/<pid>/stat field 2 is the command name: install.sh must count the fields from its last ")"
    from k3code import update as upd

    odd = tmp_path / "a) (b c"
    shutil.copy(shutil.which("sleep") or "/bin/sleep", odd)
    holder = subprocess.Popen([str(odd), "30"])
    try:
        assert Path(f"/proc/{holder.pid}/stat").read_text().startswith(f"{holder.pid} (a) (b c) ")
        lock = _lock_of(tmp_path, holder.pid, upd._process_start(holder.pid), "tmp/k3code-tui.Ab12Cd")
        r = run(tmp_path, INSTALL, "--from-source", "--minimal", env_extra={"TMPDIR": str(tmp_path / "tmp")})
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode != 0
    assert f"is running (pid {holder.pid})" in r.stderr
    assert lock.is_dir() and (tmp_path / "tmp" / "k3code-tui.Ab12Cd").is_dir()


def test_the_install_lock_is_released_after_a_run(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--minimal").returncode == 0
    assert not (tmp_path / DATA_REL / ".install.lock").exists()
    assert run(tmp_path, INSTALL, "--from-source", "--minimal").returncode == 0  # the next run gets it


def test_a_foreign_k3code_in_bin_is_kept_unless_force(tmp_path: Path) -> None:
    mine = tmp_path / ".local" / "bin" / "k3code"
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh\necho someone else's k3code\n")
    r = run(tmp_path, INSTALL, "--from-source", "--minimal")
    assert r.returncode == 0, r.stderr
    assert "not a link into" in r.stderr
    assert not mine.is_symlink() and "someone else" in mine.read_text()
    forced = run(tmp_path, INSTALL, "--from-source", "--minimal", "--force")
    assert forced.returncode == 0, forced.stderr
    assert mine.is_symlink() and os.readlink(mine).startswith(str(tmp_path / DATA_REL))


def _mini_checkout(root: Path) -> Path:
    """A committed k3code-shaped checkout carrying this installer; edits to it give new (dirty) versions."""
    src = root / "src"
    (src / "core").mkdir(parents=True)
    (src / "install").mkdir()
    (src / "core" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (src / "VERSION").write_text("9.9.9\n")
    shutil.copy(INSTALL, src / "install" / "install.sh")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    subprocess.run([*git, "add", "-A"], check=True, env=env)
    subprocess.run([*git, "commit", "-qm", "init"], check=True, env=env)
    return src


def _fake_systemd(root: Path, home: Path) -> tuple[Path, Path, Path]:
    """A k3code.service unit file plus a systemctl stub: active, MainPID read from a file, every call logged."""
    unit = home / ".config" / "systemd" / "user" / "k3code.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/bin/true\n")
    calls, pidfile = root / "systemctl.log", root / "mainpid"
    pidfile.write_text("0\n")
    body = f'echo "systemctl $*" >>"{calls}"\ncase "$*" in *show*MainPID*) cat "' + str(pidfile) + '" ;; esac\nexit 0\n'
    return stub_bin(root, "systemctl", body), calls, pidfile


@linux_only
def test_activation_restarts_a_running_daemon_and_keeps_its_version(tmp_path: Path) -> None:
    src = _mini_checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    stubs, calls, pidfile = _fake_systemd(tmp_path, home)
    versions = home / DATA_REL / "versions"

    def install() -> subprocess.CompletedProcess[str]:
        r = run(home, src / "install" / "install.sh", "--from-source", "--minimal", path_front=stubs)
        assert r.returncode == 0, r.stderr
        return r

    install()
    v1 = (home / DATA_REL / "current").resolve().name
    (src / "VERSION").write_text("9.9.9\n\n")  # uncommitted edit: a second version
    install()
    log = calls.read_text()
    assert "systemctl --user reset-failed k3code.service" in log
    assert "systemctl --user restart k3code.service" in log

    daemon = subprocess.Popen(["sleep", "60"], cwd=versions / v1)  # the "daemon" still runs from the first version
    try:
        pidfile.write_text(f"{daemon.pid}\n")
        (src / "VERSION").write_text("9.9.9\n\n\n")  # a third version: v1 is neither current nor previous
        r = install()
        assert (versions / v1).is_dir(), r.stderr
        assert "still executes from it" in r.stderr
    finally:
        daemon.kill()
        daemon.wait()
    restarts = calls.read_text().count("restart k3code.service")
    install()  # the same version again: nothing switched, so no restart
    assert calls.read_text().count("restart k3code.service") == restarts


# mv stand-ins: GNU mv (-T), BSD/macOS mv (no -T; -h does the same), and an mv with neither (the ln -sfn fallback).
# Each logs where DATA/current points at the moment it is asked to replace it, then does the move with the real mv.
MV_FLAVOURS = {
    "gnu": "",
    "bsd": '  -T) echo "mv: illegal option -- T" >&2; exit 64 ;;\n  -h) shift; exec "$REAL_MV" -T "$@" ;;\n',
    "none": '  -T | -h) echo "mv: illegal option" >&2; exit 64 ;;\n',
}


@pytest.mark.parametrize("flavour", sorted(MV_FLAVOURS))
def test_switching_versions_replaces_current_in_one_rename(tmp_path: Path, flavour: str) -> None:
    real_mv = shutil.which("mv")
    assert real_mv
    seen = tmp_path / "mv.log"
    body = (
        f'REAL_MV="{real_mv}"\nfor a; do last=$a; done\n'
        f'case "$last" in */current) printf "%s %s\\n" "$1" "$(readlink "$last")" >>"{seen}" ;; esac\n'
        f'case "$1" in\n{MV_FLAVOURS[flavour]}esac\nexec "$REAL_MV" "$@"\n'
    )
    stubs = stub_bin(tmp_path, "mv", body)
    src = _mini_checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    current = home / DATA_REL / "current"
    assert run(home, src / "install" / "install.sh", "--from-source", "--minimal", path_front=stubs).returncode == 0
    v1 = os.readlink(current)
    (src / "VERSION").write_text("9.9.9\n\n")  # uncommitted edit: a second version
    r = run(home, src / "install" / "install.sh", "--from-source", "--minimal", path_front=stubs)
    assert r.returncode == 0, r.stderr
    v2 = os.readlink(current)
    assert v2 != v1 and Path(v2).name in r.stderr and (Path(v2) / ".complete").is_file()
    assert not [p.name for p in (home / DATA_REL).iterdir() if p.name.startswith(".current.")]  # no temp link left
    if flavour != "none":
        # the rename over the old link is what switches versions: until that instant readers still see v1
        assert f"{'-T' if flavour == 'gnu' else '-h'} {v1}" in seen.read_text().splitlines()


def _path_without(root: Path, name: str) -> str:
    """This PATH with NAME hidden: each directory holding NAME is replaced by a link farm of everything else."""
    dirs = []
    for i, d in enumerate(os.environ["PATH"].split(os.pathsep)):
        if d and (Path(d) / name).exists():
            farm = root / f"path-without-{name}-{i}"
            farm.mkdir()
            for entry in Path(d).iterdir():
                if entry.name != name:
                    (farm / entry.name).symlink_to(entry)
            d = str(farm)
        dirs.append(d)
    return os.pathsep.join(dirs)


@linux_only
@pytest.mark.skipif(shutil.which("setsid") is None, reason="needs util-linux setsid")
@pytest.mark.parametrize("answer", ["y", "n"])
def test_bwrap_install_is_asked_on_the_terminal_though_stdin_is_not_one(tmp_path: Path, answer: str) -> None:
    # install.sh runs main with stdin from /dev/null (curl | sh has the script there): the question about sudo goes
    # to the controlling terminal, /dev/tty, and only a yes typed there runs sudo
    sudo_log = tmp_path / "sudo.log"
    stubs = stub_bin(tmp_path, "sudo", f'echo "sudo $*" >>"{sudo_log}"\nexit 0\n')
    stub_bin(tmp_path, "dnf", "exit 0\n")  # a package manager to name; the sudo stub never runs it
    env = {
        "PATH": f"{stubs}{os.pathsep}{_path_without(tmp_path, 'bwrap')}",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "K3_STUB_VENV": "1",
        "K3_SKIP_TUI": "1",
        "K3_SKIP_GO": "1",
        "K3_NO_GH": "1",
    }  # no K3_NO_DOWNLOAD: that means --no-install-deps, which never offers bubblewrap
    master, slave = os.openpty()
    try:
        # setsid --ctty: a new session whose controlling terminal is the pty, so the installer can open /dev/tty
        proc = subprocess.Popen(
            ["setsid", "--ctty", "sh", str(INSTALL), "--from-source", "--minimal"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
        )
        os.close(slave)
        slave = -1
        out, answered = b"", False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 1)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # EIO: every writer to the terminal is gone
                    break
                if not chunk:
                    break
                out += chunk
                if not answered and b"[y/N]" in out:
                    os.write(master, answer.encode() + b"\n")
                    answered = True
            elif proc.poll() is not None:
                break
        if proc.poll() is None:
            proc.kill()
        rc = proc.wait()
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
    text = out.decode(errors="replace")
    assert rc == 0, text
    assert "Install bubblewrap now with sudo" in text
    if answer == "y":
        assert "sudo dnf install -y bubblewrap" in sudo_log.read_text()
    else:
        assert not sudo_log.exists()
        assert "needs root to install" in text


def test_no_systemctl_call_without_a_k3code_unit(tmp_path: Path) -> None:
    calls = tmp_path / "systemctl.log"
    stubs = stub_bin(tmp_path, "systemctl", f'echo "systemctl $*" >>"{calls}"\nexit 0\n')
    assert run(tmp_path, INSTALL, "--from-source", "--minimal", path_front=stubs).returncode == 0
    assert not calls.exists()


def test_purge_without_yes_and_without_a_terminal_is_refused(tmp_path: Path) -> None:
    assert run(tmp_path, INSTALL, "--from-source", "--minimal").returncode == 0
    (tmp_path / ".k3code").mkdir(exist_ok=True)
    (tmp_path / ".k3code" / "config.yaml").write_text("x: 1\n")
    r = run(tmp_path, UNINSTALL, "--purge")  # a new session: no terminal to confirm on
    assert r.returncode != 0
    assert "--yes" in r.stderr
    assert (tmp_path / ".k3code" / "config.yaml").is_file()
    assert (tmp_path / DATA_REL / "current").is_symlink()  # refused before anything was removed


def test_uninstall_fallback_removes_both_units(tmp_path: Path) -> None:
    # no k3code to run `k3code service uninstall`: the script disables and removes the daemon and its recovery unit
    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    for name in ("k3code.service", "k3code-recover.service"):
        (units / name).write_text("[Unit]\n")
    calls = tmp_path / "systemctl.log"
    stubs = stub_bin(tmp_path, "systemctl", f'echo "systemctl $*" >>"{calls}"\nexit 0\n')
    r = run(tmp_path, UNINSTALL, path_front=stubs)
    assert r.returncode == 0, r.stderr
    assert not (units / "k3code.service").exists()
    assert not (units / "k3code-recover.service").exists()
    log = calls.read_text()
    assert "disable --now k3code.service k3code-recover.service" in log
    assert "daemon-reload" in log


def test_uninstall_with_a_broken_k3code_prints_one_line_not_a_traceback(tmp_path: Path) -> None:
    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    for name in ("k3code.service", "k3code-recover.service"):
        (units / name).write_text("[Unit]\n")
    broken = tmp_path / ".local" / "bin" / "k3code"
    broken.parent.mkdir(parents=True)
    broken.write_text(
        "#!/bin/sh\necho 'Traceback (most recent call last):' >&2\necho 'ModuleNotFoundError: k3code' >&2\nexit 1\n"
    )
    broken.chmod(0o755)
    calls = tmp_path / "systemctl.log"
    stubs = stub_bin(tmp_path, "systemctl", f'echo "systemctl $*" >>"{calls}"\nexit 0\n')
    r = run(tmp_path, UNINSTALL, path_front=stubs)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr and "ModuleNotFoundError" not in r.stderr
    assert r.stderr.count("k3code is not runnable; removing units directly") == 1
    assert not (units / "k3code.service").exists() and not (units / "k3code-recover.service").exists()
    assert "disable --now k3code.service k3code-recover.service" in calls.read_text()


def _git(src: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(src), *args]
    return subprocess.run(cmd, check=True, env=env, capture_output=True, text=True).stdout.strip()


def test_stable_channel_skips_pre_release_tags(tmp_path: Path) -> None:
    src = _source_repo(tmp_path)
    (src / "VERSION").write_text("0.0.2\n")
    _git(src, "commit", "-qam", "rc")
    _git(src, "tag", "v0.0.2-rc1")  # sorts above v0.0.1, but is a pre-release
    home = tmp_path / "home"
    home.mkdir()
    r = run(home, INSTALL, "--from-git", f"file://{src}", "--minimal")
    assert r.returncode == 0, r.stderr
    assert (home / DATA_REL / "current" / ".ref").read_text().strip() == "v0.0.1"


def test_a_short_sha_is_resolved_or_refused_clearly(tmp_path: Path) -> None:
    src = _source_repo(tmp_path)  # its first commit is the tag v0.0.1
    (src / "VERSION").write_text("0.0.2\n")
    _git(src, "commit", "-qam", "second")
    middle = _git(src, "rev-parse", "HEAD")  # no branch or tag points here
    (src / "VERSION").write_text("0.0.3\n")
    _git(src, "commit", "-qam", "third")
    tip = _git(src, "rev-parse", "HEAD")
    home = tmp_path / "home"
    home.mkdir()
    r = run(home, INSTALL, "--from-git", f"file://{src}", "--ref", tip[:9], "--minimal")
    assert r.returncode == 0, r.stderr
    assert (home / DATA_REL / "current" / ".ref").read_text().strip() == tip
    old = run(home, INSTALL, "--from-git", f"file://{src}", "--ref", middle[:9], "--minimal")
    assert old.returncode != 0
    assert "full 40-character SHA" in old.stderr


def _download_stub(root: Path, files: dict[str, bytes]) -> tuple[Path, Path]:
    """A curl stand-in that serves ``files`` by the last path segment of the URL (go.dev's release list as
    ``releases.json``), refuses anything else, and logs every URL."""
    served = root / "served"
    served.mkdir(parents=True)
    for name, data in files.items():
        (served / name).write_bytes(data)
    log = root / "curl.log"
    body = (
        'out=""; url=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift ;; https://*) url=$1 ;; esac; shift; done\n'
        f'echo "$url" >>"{log}"\n'
        'case "$url" in *mode=json*) f=releases.json ;; *) f=${url##*/} ;; esac\n'
        f'[ -f "{served}/$f" ] || exit 22\n'
        f'cp "{served}/$f" "$out"\n'
    )
    return stub_bin(root, "curl", body), log


def _tar_gz(entries: dict[str, str]) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in entries.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _no_uv_path() -> str:
    return os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / "uv").exists())


@pytest.mark.skipif(sys.platform != "linux" or os.uname().machine != "x86_64", reason="fixture names the linux x64 uv")
@pytest.mark.parametrize("published", ["match", "mismatch"])
def test_a_uv_archive_off_the_pinned_hash_is_refused(tmp_path: Path, published: str) -> None:
    import hashlib

    archive = _tar_gz({"uv-x86_64-unknown-linux-gnu/uv": "#!/bin/sh\necho planted\n"})
    digest = hashlib.sha256(archive).hexdigest() if published == "match" else "0" * 64
    name = "uv-x86_64-unknown-linux-gnu.tar.gz"
    curl, log = _download_stub(tmp_path, {name: archive, name + ".sha256": f"{digest}  {name}\n".encode()})
    home = tmp_path / "home"
    home.mkdir()
    r = run(
        home,
        INSTALL,
        "--from-source",
        "--minimal",
        env_extra={"PATH": _no_uv_path()},
        drop=("K3_NO_DOWNLOAD",),
        path_front=curl,
    )
    assert r.returncode != 0
    assert "does not match its checksum" in r.stderr
    assert not (home / ".local" / "bin" / "uv").exists()
    assert "https://github.com/astral-sh/uv/releases/download/" in log.read_text()
    assert "astral.sh/uv/install.sh" not in log.read_text()


GO_OS = {"linux": "linux", "darwin": "darwin"}.get(sys.platform, "")
GO_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(os.uname().machine, "")


@pytest.mark.skipif(not GO_OS or not GO_ARCH, reason="needs a platform go.dev builds for")
@pytest.mark.parametrize("listed", ["right", "wrong"])
def test_an_old_go_gets_a_checked_private_go(tmp_path: Path, listed: str) -> None:
    import hashlib
    import json

    want = next(ln.split()[1] for ln in (REPO / "panes" / "go.mod").read_text().splitlines() if ln.startswith("go "))
    fake_go = (
        "#!/bin/sh\n"
        f'case "$1" in version) echo "go version go{want} {GO_OS}/{GO_ARCH}" ;;\n'
        'build) while [ $# -gt 0 ]; do [ "$1" = -o ] && touch "$2"; shift; done ;; esac\n'
    )
    archive = _tar_gz({"go/bin/go": fake_go})
    name = f"go{want}.{GO_OS}-{GO_ARCH}.tar.gz"
    digest = hashlib.sha256(archive).hexdigest() if listed == "right" else "1" * 64
    releases = [{"version": f"go{want}", "files": [{"filename": name, "os": GO_OS, "arch": GO_ARCH, "sha256": digest}]}]
    curl, log = _download_stub(tmp_path, {name: archive, "releases.json": json.dumps(releases, indent=1).encode()})
    # the go on PATH is too old for panes/go.mod
    old = stub_bin(tmp_path, "go", 'case "$1" in version) echo "go version go1.20.1 x/y" ;; *) exit 1 ;; esac\n')
    r = run(tmp_path, INSTALL, "--from-source", "--minimal", drop=("K3_NO_DOWNLOAD", "K3_SKIP_GO"), path_front=old)
    assert r.returncode == 0, r.stderr
    private = tmp_path / DATA_REL / "go" / f"go{want}" / "bin" / "go"
    assert f"https://go.dev/dl/{name}" in log.read_text() or listed == "wrong"
    assert "proxy.golang.org" not in log.read_text()
    if listed == "right":
        assert private.is_file()
        assert (tmp_path / DATA_REL / "current" / "bin" / "k3").is_file()  # built with the private go
    else:
        assert not private.exists()
        assert "did not match the sha256 go.dev lists" in r.stderr


def test_credentials_in_urls_never_reach_the_log(tmp_path: Path) -> None:
    real_git = shutil.which("git")
    url = "https://alice:s3cr3t-token@example.invalid/x.git"
    body = (
        f'case "$*" in *fetch*) echo "fatal: unable to access \'{url}/\': Could not resolve host" >&2\n'
        "  exit 128 ;; esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    stubs = stub_bin(tmp_path, "git", body)
    r = run(tmp_path, INSTALL, "--from-git", url, "--ref", "main", path_front=stubs)
    assert r.returncode != 0
    log = tmp_path / DATA_REL / "install.log"
    text = log.read_text()
    assert "https://***@example.invalid/x.git" in text  # the args line and the fetch error, redacted
    assert "s3cr3t" not in text and "s3cr3t" not in r.stderr
    assert log.stat().st_mode & 0o777 == 0o600


def test_downloads_are_https_only(tmp_path: Path) -> None:
    calls = tmp_path / "curl.log"
    curl = stub_bin(tmp_path, "curl", f'echo "curl $*" >>"{calls}"\nexit 22\n')
    r = run(
        tmp_path,
        INSTALL,
        "--from-source",
        "--minimal",
        env_extra={"PATH": _no_uv_path()},
        drop=("K3_NO_DOWNLOAD",),
        path_front=curl,
    )
    assert r.returncode != 0  # no uv, and its download "failed"
    assert "--proto =https --tlsv1.2" in calls.read_text()


def test_from_source_builds_the_tui_outside_the_checkout(tmp_path: Path) -> None:
    src = _mini_checkout(tmp_path)
    (src / "tui").mkdir()
    (src / "tui" / "package.json").write_text("{}\n")
    tools = stub_bin(tmp_path, "node", 'echo "v22.0.0"\n')
    stub_bin(
        tmp_path,
        "npm",
        'case "$*" in ci*) mkdir -p node_modules ;; "run build") mkdir -p dist && echo built >dist/entry.js ;; esac\n',
    )
    r = run(
        tmp_path, src / "install" / "install.sh", "--from-source", "--minimal", drop=("K3_SKIP_TUI",), path_front=tools
    )
    assert r.returncode == 0, r.stderr
    assert (tmp_path / DATA_REL / "current" / "tui" / "dist" / "entry.js").read_text() == "built\n"
    assert not (src / "tui" / "node_modules").exists()
    assert not (src / "tui" / "dist").exists()


def test_the_tui_build_dir_is_recorded_in_the_lock_with_a_single_slash(tmp_path: Path) -> None:
    # the recording side of the leftover-build-dir cleanup: with a TMPDIR ending in "/" the lock must name
    # "<tmp>/k3code-tui.X", the form the next run's strict prefix check accepts
    src = _mini_checkout(tmp_path)
    (src / "tui").mkdir()
    (src / "tui" / "package.json").write_text("{}\n")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    seen = tmp_path / "seen"
    seen.mkdir()
    lock = tmp_path / DATA_REL / ".install.lock"
    tools = stub_bin(tmp_path, "node", 'echo "v22.0.0"\n')
    stub_bin(
        tmp_path,
        "npm",
        f'case "$*" in ci*) cp "{lock}/tui_tmp" "{seen}/tui_tmp"; pwd >"{seen}/cwd"; mkdir -p node_modules ;;'
        ' "run build") mkdir -p dist ;; esac\n',
    )
    r = run(
        tmp_path,
        src / "install" / "install.sh",
        "--from-source",
        "--minimal",
        env_extra={"TMPDIR": f"{tmp}/"},
        drop=("K3_SKIP_TUI",),
        path_front=tools,
    )
    assert r.returncode == 0, r.stderr
    recorded = (seen / "tui_tmp").read_text().strip()
    assert re.fullmatch(re.escape(f"{tmp}/") + r"k3code-tui\.[A-Za-z0-9]+", recorded), recorded
    assert (seen / "cwd").read_text().strip().startswith(recorded + "/")


@linux_only
def test_the_installer_writes_its_start_time_before_its_pid(tmp_path: Path) -> None:
    # a kill between the two writes must not leave a pid without a start time (a recycled pid would keep the lock)
    lock = tmp_path / DATA_REL / ".install.lock"
    mark = tmp_path / "mark"
    # proc_start reads /proc/PID/stat through cat: note whether the pid file exists at that moment
    tools = stub_bin(
        tmp_path,
        "cat",
        f'case "$1" in /proc/*/stat) if [ -e "{lock}/pid" ]; then echo pid-first; else echo start-first; fi'
        f' >"{mark}" ;; esac\nexec "$(PATH=/usr/bin:/bin command -v cat)" "$@"\n',
    )
    r = run(tmp_path, INSTALL, "--from-source", "--minimal", path_front=tools)
    assert r.returncode == 0, r.stderr
    assert mark.read_text().strip() == "start-first"


def test_without_a_checksum_tool_a_no_git_checkout_is_refused(tmp_path: Path) -> None:
    # an empty checksum would name every checkout X.Y.Z-src.tree and a second one would look installed already
    nogit = stub_bin(tmp_path, "git", 'echo "xcode-select: note: no developer tools were found" >&2\nexit 1\n')
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "install").mkdir()
    (src / "core" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (src / "VERSION").write_text("9.9.9\n")
    shutil.copy(INSTALL, src / "install" / "install.sh")
    farm = tmp_path / "farm"
    farm.mkdir()
    for d in os.environ["PATH"].split(os.pathsep):
        if d and Path(d).is_dir():
            for entry in Path(d).iterdir():
                if entry.name not in ("sha256sum", "shasum", "git") and not (farm / entry.name).exists():
                    (farm / entry.name).symlink_to(entry)
    home = tmp_path / "home"
    home.mkdir()
    r = run(home, src / "install" / "install.sh", "--from-source", "--minimal", env_extra={"PATH": f"{nogit}:{farm}"})
    assert r.returncode != 0
    assert "neither sha256sum nor shasum is available" in r.stderr
    assert not (home / DATA_REL / "current").exists()


def test_without_git_a_github_tag_installs_from_its_archive(tmp_path: Path) -> None:
    sha_rel, sha_rc = "a" * 40, "b" * 40
    advert = (
        "001e# service=git-upload-pack\n0000"
        f"0099{sha_rc} HEAD\0multi_ack symref=HEAD:refs/heads/main\n"
        f"003f{sha_rc} refs/heads/main\n"
        f"0041{sha_rel} refs/tags/v0.0.1\n"
        f"0044{sha_rc} refs/tags/v0.0.2-rc1\n"
        "0000"
    )
    archive = _tar_gz(
        {"k3fake-0.0.1/VERSION": "0.0.1\n", "k3fake-0.0.1/core/pyproject.toml": "[project]\nname = 'x'\n"}
    )
    curl, log = _download_stub(tmp_path, {"refs?service=git-upload-pack": advert.encode(), "v0.0.1": archive})
    # the macOS /usr/bin/git stub: present on PATH, but fails without the developer tools
    stub_bin(tmp_path, "git", 'echo "xcode-select: note: no developer tools were found" >&2\nexit 1\n')
    r = run(tmp_path, INSTALL, "--from-git", "https://github.com/alice/k3fake", "--minimal", path_front=curl)
    assert r.returncode == 0, r.stderr
    current = tmp_path / DATA_REL / "current"
    assert (current / ".ref").read_text().strip() == "v0.0.1"  # the newest tag that is not a pre-release
    assert current.resolve().name == "0.0.1-src.aaaaaaa"
    assert "https://codeload.github.com/alice/k3fake/tar.gz/refs/tags/v0.0.1" in log.read_text()

    other = run(tmp_path, INSTALL, "--from-git", "https://example.invalid/x.git", "--minimal", path_front=curl)
    assert other.returncode != 0
    assert "git is needed" in other.stderr


def test_k3_allow_root_lets_a_root_run_install_like_the_flag(tmp_path: Path) -> None:
    # `k3code update` re-runs install.sh as root for a root install and cannot pass a flag an older ref would reject
    ids = stub_bin(tmp_path, "id", 'case "$1" in -u) echo 0 ;; -un) echo root ;; *) echo "uid=0(root)" ;; esac\n')
    home = tmp_path / "home"
    home.mkdir()
    ok = run(home, INSTALL, "--from-source", "--minimal", env_extra={"K3_ALLOW_ROOT": "1"}, path_front=ids)
    assert ok.returncode == 0, ok.stderr
    assert (home / DATA_REL / "current").is_symlink()


def _fetch_only(root: Path, url: str, tool_body: str, tool: str) -> subprocess.CompletedProcess[str]:
    """Run install.sh's fetch() alone, with ``tool`` the only downloader on a PATH that holds nothing else."""
    root.mkdir(parents=True, exist_ok=True)
    text = INSTALL.read_text().splitlines()
    keep = [ln for ln in text if ln.startswith("have() ")]
    start = next(i for i, ln in enumerate(text) if ln.startswith("fetch() {"))
    end = text.index("}", start)
    script = root / "fetch.sh"
    script.write_text("\n".join([*keep, *text[start : end + 1], f'fetch "$1" "{root}/out"', ""]))
    bin_dir = stub_bin(root, tool, tool_body)
    return subprocess.run(
        ["/bin/sh", str(script), url], env={"PATH": str(bin_dir)}, capture_output=True, text=True, check=False
    )


# BusyBox wget (the only downloader on a stock Alpine) rejects --https-only: it must not be passed
BUSYBOX_WGET = (
    'for a in "$@"; do case "$a" in --https-only) echo "wget: unrecognized option" >&2; exit 1 ;; esac; done\n'
)


def test_fetch_works_with_a_busybox_wget_and_refuses_plain_http(tmp_path: Path) -> None:
    good = _fetch_only(tmp_path / "g", "https://example.invalid/x", BUSYBOX_WGET, "wget")
    assert good.returncode == 0, good.stderr
    plain = _fetch_only(tmp_path / "p", "http://example.invalid/x", 'touch "$0.called"\n', "wget")
    assert plain.returncode != 0
    assert not (tmp_path / "p" / "stubbin" / "wget.called").exists()  # refused before any downloader ran


def test_a_bad_signature_on_the_node_checksum_list_skips_the_tui(tmp_path: Path) -> None:
    served = tmp_path / "served"
    served.mkdir()
    (served / "SHASUMS256.txt").write_text(f"{'0' * 64}  node-v22.1.0-linux-x64.tar.gz\n")
    (served / "SHASUMS256.txt.asc").write_text("signature\n")
    log = tmp_path / "curl.log"
    curl_body = (
        'out=""; url=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift ;; https://*) url=$1 ;; esac; shift; done\n'
        f'echo "$url" >>"{log}"\n'
        f'[ -f "{served}/${{url##*/}}" ] || exit 22\n'
        f'cp "{served}/${{url##*/}}" "$out"\n'
    )
    stubs = stub_bin(tmp_path, "curl", curl_body)
    stub_bin(tmp_path, "node", "exit 1\n")  # no usable node on this machine
    stub_bin(tmp_path, "gpg", 'echo "[GNUPG:] BADSIG 0123 Node Release"\nexit 1\n')
    home = tmp_path / "home"
    home.mkdir()
    r = run(
        home,
        INSTALL,
        "--from-source",
        "--minimal",
        env_extra={"K3_SKIP_TUI": "0", "K3_NO_DOWNLOAD": "0"},
        path_front=stubs,
    )
    assert "signature on nodejs.org's SHASUMS256.txt is bad" in r.stderr, r.stderr
    assert "SHASUMS256.txt.asc" in log.read_text()
    assert not (home / DATA_REL / "node").exists()
