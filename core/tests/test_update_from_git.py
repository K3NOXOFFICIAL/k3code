"""`k3code update` on an install made with `install.sh --from-git` (the default): such an install records only the
ref it was built from (`<version dir>/.ref`) and keeps no checkout, and no release is published, so `update` used to
fail with "no releases found on this channel". It now rebuilds from the newest commit of that ref.

Everything runs against a local bare repository with a stub `install/install.sh` (no network, no real install), and
`activate`/`prune` are replaced so no daemon is restarted."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import update as upd
from k3code.cli import cli
from k3code.paths import user_config_path

STAGED = "0.1.0-src.fffffff"

STUB_INSTALLER = """#!/bin/sh
# stand-in for install.sh: records how it was called and from where, then prints a version name
printf '%s\\n' "$@" >"$K3_STUB_ARGS"
printf '%s\\n' "$(cd "$(dirname "$0")/.." && pwd)" >"$K3_STUB_DIR"
printf '%s\\n' "${K3CODE_DATA:-}" >"$K3_STUB_DATA"
printf '%s\\n' "${K3_ALLOW_ROOT:-unset}" >"$K3_STUB_ARGS.root"
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
