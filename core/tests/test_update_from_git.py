"""`k3code update` on an install made with `install.sh --from-git` (the default): such an install records only the
ref it was built from (`<version dir>/.ref`) and keeps no checkout, and no release is published, so `update` used to
fail with "no releases found on this channel". It now rebuilds from the newest commit of that ref.

Everything runs against a local bare repository with a stub `install/install.sh` (no network, no real install), and
`activate`/`prune` are replaced so no daemon is restarted."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import update as upd
from k3code.cli import cli
from k3code.paths import user_config_path

STAGED = "0.1.0-src.fffffff"
PROGRESS = "k3code-install: installing the k3code core"

STUB_INSTALLER = """#!/bin/sh
# stand-in for install.sh: records how it was called and from where, then prints a version name
printf '%s\\n' "$@" >"$K3_STUB_ARGS"
printf '%s\\n' "$(cd "$(dirname "$0")/.." && pwd)" >"$K3_STUB_DIR"
printf '%s\\n' "${K3CODE_DATA:-}" >"$K3_STUB_DATA"
printf '%s\\n' "${K3_ALLOW_ROOT:-unset}" >"$K3_STUB_ARGS.root"
echo "k3code-install: installing the k3code core" >&2
echo "npm warn deprecated noise that only a failure message needs" >&2
if [ -n "${K3_STUB_GATE:-}" ]; then # build on only once the test has seen the line above (it gives up after 10 s)
  i=0
  while [ ! -e "$K3_STUB_GATE" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
  [ -e "$K3_STUB_GATE" ] || { echo "the progress line was not shown while the installer ran" >&2; exit 4; }
fi
cp "$(dirname "$0")/../../owner" "$K3_STUB_ARGS.owner" 2>/dev/null || :
if [ -n "${K3_STUB_FAIL:-}" ]; then
  echo "building the TUI" >&2
  echo "npm ERR! something broke" >&2
  exit 3
fi
echo "log line on stdout"
echo "${K3_STUB_VERSION:-0.1.0-src.fffffff}"
"""


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(root: Path, *args: str) -> str:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    ident += ["-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]
    r = subprocess.run(["git", *ident, *args], cwd=root, env=_clean_env(), capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A bare repository (file:// URL) whose `main` holds a stub installer, set as `update.url`."""
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    work = tmp_path / "work"
    bare = tmp_path / "remote.git"
    assert work.resolve().is_relative_to(tmp_path.resolve())
    work.mkdir()
    _git(work, "init", "-q")
    (work / "install").mkdir()
    (work / "install" / "install.sh").write_text(STUB_INSTALLER)
    (work / "VERSION").write_text("0.1.0\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "one")
    _git(work, "commit", "-q", "--allow-empty", "-m", "two")
    _git(work, "tag", "-a", "v0.2.0", "-m", "annotated", "HEAD~1")
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    head = _git(work, "rev-parse", "HEAD")
    url = bare.as_uri()
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text(f"update:\n  url: {url}\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("ARGS", "DIR", "DATA"):
        monkeypatch.setenv(f"K3_STUB_{name}", str(logs / name.lower()))
    return {"url": url, "head": head, "work": work, "logs": logs, "tag_commit": _git(work, "rev-parse", "HEAD~1")}


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("K3CODE_DATA", str(d))
    (d / "versions").mkdir(parents=True)
    return d


def _install(data: Path, sha: str, ref: str | None = "main") -> str:
    """A complete git-built version `0.1.0-src.<sha7>` made current, with `.ref` when given."""
    ver = f"0.1.0-src.{sha[:7]}"
    vdir = data / "versions" / ver
    vdir.mkdir(parents=True)
    (vdir / ".complete").write_text(ver + "\n")
    if ref is not None:
        (vdir / ".ref").write_text(ref + "\n")
    (data / "current").symlink_to(vdir)
    return ver


@pytest.fixture
def no_release(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No release is published; activate/prune are recorded instead of switching versions and restarting units."""
    activated: list[str] = []
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: None)
    monkeypatch.setattr(upd, "activate", lambda v: activated.append(v) or upd.UpdateResult(True, v, f"updated to {v}"))
    monkeypatch.setattr(upd, "prune", lambda: activated.append("pruned") or [])
    return activated


def _no_update(*_a, **_k):
    pytest.fail("must not stage a new version")


# -- CLI --------------------------------------------------------------------------


def test_an_up_to_date_git_install_says_so_and_runs_no_installer(remote, data, no_release, monkeypatch):
    _install(data, remote["head"])
    monkeypatch.setattr(upd, "update_from_git", _no_update)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert "Already up to date." in r.output and "no releases found" not in r.output
    assert "made from git (main of " in r.output
    assert no_release == [] and not (remote["logs"] / "args").exists()


def test_a_newer_head_is_staged_by_the_refs_own_installer_and_activated(remote, data, no_release):
    _install(data, "1234567aaaa")
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert (remote["logs"] / "args").read_text().splitlines() == [
        "--from-git",
        remote["url"],
        "--ref",
        "main",
        "--yes",
        "--no-setup",
        "--no-activate",
        "--print-version",
    ]
    assert f"latest:  {remote['head'][:7]} (main)" in r.output and f"updated to {STAGED}" in r.output
    assert no_release == [STAGED, "pruned"]
    # the installer ran from a temporary clone under the data dir (not versions/), which is gone again
    clone = Path((remote["logs"] / "dir").read_text().strip())
    assert clone.parent.parent == data and clone.parent.name.startswith(".update-git-") and not clone.exists()
    assert (remote["logs"] / "data").read_text().strip() == str(data)
    assert sorted(p.name for p in data.iterdir()) == ["current", "versions"]


def test_the_installers_progress_is_shown_between_latest_and_updated(remote, data, no_release):
    _install(data, "1234567aaaa")
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert r.output.index("latest:") < r.output.index(PROGRESS) < r.output.index(f"updated to {STAGED}")
    assert "npm warn" not in r.output  # the build tools' own output stays out of the way (a failure shows its tail)


def test_progress_lines_arrive_while_the_installer_is_still_running(remote, data, monkeypatch, tmp_path):
    gate = tmp_path / "gate"
    monkeypatch.setenv("K3_STUB_GATE", str(gate))
    seen: list[str] = []

    def progress(line: str) -> None:
        seen.append(line)
        gate.touch()  # the installer builds on only now: output read after it exits would never get here

    assert upd.update_from_git(remote["url"], "main", progress=progress) == STAGED
    assert seen == [PROGRESS]


def test_a_failing_progress_display_does_not_stop_the_installer_or_its_later_lines(tmp_path):
    installer = tmp_path / "install" / "install.sh"
    installer.parent.mkdir()
    installer.write_text(
        '#!/bin/sh\necho "k3code-install: one" >&2\necho "k3code-install: two" >&2\necho 0.1.0-src.abc\n'
    )
    seen: list[str] = []

    def progress(line: str) -> None:
        seen.append(line)
        raise OSError("stderr is gone")

    assert upd.update_from_source(tmp_path, pull=False, progress=progress) == "0.1.0-src.abc"
    assert seen == ["k3code-install: one", "k3code-install: two"]


def _dead_pid() -> int:
    return int(subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True, check=True).stdout)


def _staging_dir(data: Path, name: str, owner: str | None, *, age: float = 0.0) -> Path:
    """A `.update-git-*` directory as a `k3code update` leaves it when it is SIGKILLed: a clone, an owner when known."""
    d = data / f".update-git-{name}"
    (d / "src" / "install").mkdir(parents=True)
    (d / "src" / "install" / "install.sh").write_text("#!/bin/sh\n")
    if owner is not None:
        (d / "owner").write_text(owner)
    if age:
        old = time.time() - age
        os.utime(d, (old, old))
    return d


def test_a_killed_updates_staging_clone_is_removed_by_the_next_update(remote, data, no_release):
    _install(data, "1234567aaaa")
    killed = _staging_dir(data, "dead", f"{_dead_pid()}\n{upd._process_start(os.getpid())}\n")
    reused = _staging_dir(data, "reused", f"{os.getpid()}\n0\n")  # its pid now belongs to this (later) process
    ownerless = _staging_dir(data, "old", None, age=120)  # from before the owner file, or killed before writing it
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert not killed.exists() and not reused.exists() and not ownerless.exists()
    assert sorted(p.name for p in data.iterdir()) == ["current", "versions"]


def test_a_running_updates_staging_clone_is_left_alone(remote, data, no_release):
    _install(data, "1234567aaaa")
    start = upd._process_start(os.getpid())
    live = _staging_dir(data, "live", f"{os.getpid()}\n{start}\n", age=3600)  # old, but its owner is running
    fresh = _staging_dir(data, "fresh", None)  # between mkdtemp and the owner file of an update that just started
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert live.is_dir() and fresh.is_dir()


def test_a_staging_clone_names_its_owner_while_the_installer_runs(remote, data, no_release):
    _install(data, "1234567aaaa")
    assert upd.update_from_git(remote["url"], "main") == STAGED
    pid, _, start = (remote["logs"] / "args.owner").read_text().partition("\n")
    assert pid == str(os.getpid()) and start.strip() == upd._process_start(os.getpid())


@pytest.mark.parametrize("uid", [0, 1000])
def test_the_installer_is_told_to_allow_root_only_when_updating_as_root(remote, data, monkeypatch, uid):
    monkeypatch.delenv("K3_ALLOW_ROOT", raising=False)
    monkeypatch.setattr(upd.os, "geteuid", lambda: uid)
    assert upd.update_from_git(remote["url"], "main") == STAGED
    assert (remote["logs"] / "args.root").read_text().strip() == ("1" if uid == 0 else "unset")


@pytest.mark.parametrize("uid", [0, 1000])
def test_a_source_update_passes_allow_root_only_as_root(tmp_path, monkeypatch, uid):
    seen: list[dict[str, str]] = []

    def fake_run(cmd, **kw):
        seen.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{STAGED}\n", stderr="")

    monkeypatch.delenv("K3_ALLOW_ROOT", raising=False)
    monkeypatch.setattr(upd.os, "geteuid", lambda: uid)
    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    assert upd.update_from_source(tmp_path, pull=False) == STAGED
    assert seen[0].get("K3_ALLOW_ROOT") == ("1" if uid == 0 else None)


def test_check_only_reports_current_and_latest(remote, data, no_release, monkeypatch):
    cur = _install(data, "1234567aaaa")
    monkeypatch.setattr(upd, "update_from_git", _no_update)
    r = CliRunner().invoke(cli, ["update", "--check"])
    assert r.exit_code == 0, r.output
    assert f"current: {cur}" in r.output and f"latest:  {remote['head'][:7]} (main)" in r.output
    assert "Already up to date" not in r.output and no_release == []


def test_a_pinned_commit_is_reported_without_touching_the_network(data, no_release, monkeypatch):
    sha = "a" * 40
    _install(data, sha, ref=sha)
    monkeypatch.setattr(upd, "_git", lambda *a, **k: pytest.fail("a pinned commit needs no git call"))
    monkeypatch.setattr(upd, "update_from_git", _no_update)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert f"pinned to commit {sha[:12]}" in r.output and no_release == []


def test_a_ref_gone_from_the_remote_is_a_clear_error(remote, data, no_release):
    _install(data, "1234567aaaa", ref="gone")
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and "Traceback" not in r.output
    assert "'gone' was not found on" in r.output and no_release == []


PS1 = "powershell -ExecutionPolicy Bypass -File install\\install.ps1"


def test_a_pinned_commit_hint_names_install_ps1_in_wsl(data, no_release, monkeypatch):
    sha = "a" * 40
    _install(data, sha, ref=sha)
    monkeypatch.setattr(upd, "_in_wsl", lambda: True)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert f"`{PS1} --from-git --ref Main`" in r.output and "`sh install.sh --from-git --ref Main`" in r.output


def test_a_pinned_commit_hint_outside_wsl_names_only_install_sh(data, no_release, monkeypatch):
    sha = "a" * 40
    _install(data, sha, ref=sha)
    monkeypatch.setattr(upd, "_in_wsl", lambda: False)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert "`sh install.sh --from-git --ref Main`" in r.output and "install.ps1" not in r.output


def test_a_ref_gone_hint_names_install_ps1_in_wsl(remote, data, no_release, monkeypatch):
    _install(data, "1234567aaaa", ref="gone")
    monkeypatch.setattr(upd, "_in_wsl", lambda: True)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and f"`{PS1} --from-git {remote['url']} --ref Main`" in r.output
    assert f"`sh install.sh --from-git {remote['url']} --ref Main`" in r.output


def test_the_no_checkout_no_ref_hint_names_install_ps1_in_wsl(data, no_release, monkeypatch):
    _install(data, "1234567aaaa", ref=None)
    monkeypatch.setattr(upd, "_in_wsl", lambda: True)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and f"`{PS1} --from-git`" in r.output and "`sh install.sh --from-git`" in r.output


def test_installer_command_spells_install_ps1_only_where_a_windows_user_can_run_it(monkeypatch):
    monkeypatch.setattr(upd, "_in_wsl", lambda: False)
    assert upd.installer_command("--from-git") == "`sh install.sh --from-git`"
    monkeypatch.setattr(upd, "_in_wsl", lambda: True)
    assert upd.installer_command("--from-git") == f"`{PS1} --from-git` or, inside WSL, `sh install.sh --from-git`"


def test_an_auth_failure_reaching_the_remote_gives_advice_not_a_traceback(data, no_release, monkeypatch):
    _install(data, "1234567aaaa")
    fail = subprocess.CompletedProcess([], 128, "", "fatal: could not read Username for 'https://github.com'\n")
    monkeypatch.setattr(upd.subprocess, "run", lambda *a, **k: fail)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and "Traceback" not in r.output
    assert "could not sign in" in r.output and "gh auth login" in r.output and no_release == []


def test_a_failing_installer_reports_its_last_lines_and_leaves_no_clone(remote, data, no_release, monkeypatch):
    _install(data, "1234567aaaa")
    monkeypatch.setenv("K3_STUB_FAIL", "1")
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and "the installer failed (exit 3)" in r.output and "npm ERR!" in r.output
    assert no_release == [] and not Path((remote["logs"] / "dir").read_text().strip()).exists()
    assert sorted(p.name for p in data.iterdir()) == ["current", "versions"]


def test_an_installer_that_kept_the_installed_build_is_not_activated(remote, data, no_release, monkeypatch):
    cur = _install(data, "1234567aaaa")
    monkeypatch.setenv("K3_STUB_VERSION", cur)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and f"kept the installed {cur}" in r.output and no_release == []


def test_without_ref_or_checkout_the_old_error_remains_with_a_way_out(data, no_release):
    _install(data, "1234567aaaa", ref=None)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0
    assert "no releases found on this channel" in r.output and "install.sh --from-git" in r.output


def test_a_source_checkout_still_wins_over_a_recorded_ref(data, no_release, monkeypatch, tmp_path):
    _install(data, "1234567aaaa")
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (data / "source_path").write_text(str(clone))
    calls: list[Path] = []
    monkeypatch.setattr(upd, "update_from_git", _no_update)
    monkeypatch.setattr(upd, "update_from_source", lambda src, *, pull=True: calls.append(src) or "0.1.0-src.abc")
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert calls == [clone] and f"built from {clone}" in r.output and no_release == ["0.1.0-src.abc", "pruned"]


def test_a_published_release_still_wins_over_a_recorded_ref(data, no_release, monkeypatch):
    _install(data, "1234567aaaa")
    rel = upd.Release(tag="v0.2.0", version="0.2.0", body="notes", prerelease=False, assets={})
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: rel)
    monkeypatch.setattr(upd, "install_release", lambda r, tok, **kw: None)
    monkeypatch.setattr(upd, "update_from_git", _no_update)
    monkeypatch.setattr(upd, "remote_head", _no_update)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert "latest:  0.2.0" in r.output and no_release == ["0.2.0", "pruned"]


# -- update.py --------------------------------------------------------------------


def test_remote_head_resolves_branches_and_annotated_tags_to_commits(remote):
    assert upd.remote_head(remote["url"], "main") == remote["head"]
    # the commit the tag points at (what the installer builds and names the version after), not the tag object
    assert upd.remote_head(remote["url"], "v0.2.0") == remote["tag_commit"]


def test_remote_head_does_not_take_a_ref_that_only_ends_in_the_name(remote):
    _git(remote["work"], "push", "-q", remote["url"], "HEAD:refs/heads/feature/next")
    assert upd.remote_head(remote["url"], "feature/next") == remote["head"]
    with pytest.raises(upd.SourceUpdateError, match="'next' was not found"):
        upd.remote_head(remote["url"], "next")


def test_installed_sha_and_git_ref_read_the_active_version(data):
    assert upd.git_ref() is None and upd.installed_sha() is None
    _install(data, "abcdef1234", ref="Main")
    assert upd.git_ref() == "Main" and upd.installed_sha() == "abcdef1"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (FileNotFoundError("git"), "git is not installed"),
        (subprocess.TimeoutExpired(["git"], 30), "timed out after 30 s"),
        ("fatal: unable to access 'https://x/': Could not resolve host: github.com", "no network"),
        ("remote: Repository not found.\nfatal: repository 'https://x/' not found", "could not sign in"),
        ("fatal: something else entirely", "failed:\nfatal: something else entirely"),
    ],
)
def test_git_failures_become_advice(monkeypatch, outcome, expected):
    seen: dict[str, object] = {}

    def run(*a, **k):
        seen.update(k)
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(a[0], 128, "", outcome)

    monkeypatch.setattr(upd.subprocess, "run", run)
    with pytest.raises(upd.SourceUpdateError) as e:
        upd.remote_head("https://example.invalid/r.git", "main")
    assert expected in str(e.value)
    # never waits on a credential prompt, never hangs
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0" and seen["timeout"] == upd.LS_REMOTE_TIMEOUT
    assert seen["stdin"] is subprocess.DEVNULL
