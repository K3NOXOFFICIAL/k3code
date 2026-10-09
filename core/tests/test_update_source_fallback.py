"""`k3code update` on an install built from a checkout while the repository is private (no token) or has no release:
it updates from the checkout instead of failing with a GitHub 404, pulls a Windows-made clone with Windows git when
run from WSL, and explains an authentication failure instead of raising."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import update as upd
from k3code.cli import cli


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "source_path").write_text(str(clone))
    return clone


def _deny(*_a, **_k):
    raise PermissionError("GitHub API 404: K3NOXOFFICIAL/k3code is private or does not exist; set GITHUB_TOKEN")


def test_a_private_repo_without_a_token_updates_from_the_checkout(checkout, monkeypatch):
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", _deny)
    monkeypatch.setattr(
        upd, "update_from_source", lambda src, *, pull=True: calls.append((src, pull)) or "0.1.0-src.abc"
    )
    monkeypatch.setattr(upd, "activate", lambda v: upd.UpdateResult(True, v, f"updated to {v}"))
    monkeypatch.setattr(upd, "prune", lambda: [])
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 0, r.output
    assert "404" in r.output and f"built from {checkout}" in r.output and "updated to 0.1.0-src.abc" in r.output
    assert calls == [(checkout, True)]


def test_no_release_yet_also_updates_from_the_checkout_and_no_pull_skips_git(checkout, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: None)
    monkeypatch.setattr(upd, "update_from_source", lambda src, *, pull=True: calls.append(pull) or "0.1.0-src.def")
    monkeypatch.setattr(upd, "activate", lambda v: upd.UpdateResult(True, v, f"updated to {v}"))
    monkeypatch.setattr(upd, "prune", lambda: [])
    r = CliRunner().invoke(cli, ["update", "--from-source", "--no-pull", "--yes"])
    assert r.exit_code == 0, r.output
    assert calls == [False]
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert "No release has been published yet" in r.output and calls == [False, True]


def test_check_only_reports_the_checkout_and_changes_nothing(checkout, monkeypatch):
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", _deny)
    monkeypatch.setattr(upd, "update_from_source", lambda *a, **k: pytest.fail("--check must not update"))
    r = CliRunner().invoke(cli, ["update", "--check"])
    assert r.exit_code == 0 and f"source: {checkout}" in r.output


def test_an_install_without_a_checkout_still_gets_the_token_advice(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", _deny)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and "GITHUB_TOKEN" in r.output and "Traceback" not in r.output


def test_from_wsl_a_windows_clone_is_pulled_with_windows_git(monkeypatch):
    monkeypatch.setattr(upd, "_in_wsl", lambda: True)
    monkeypatch.setattr(
        upd.shutil, "which", lambda name: "/mnt/c/Program Files/Git/cmd/git.exe" if name == "git.exe" else None
    )

    class R:
        stdout = "C:\\Users\\someone\\k3code\\k3code\n"

    monkeypatch.setattr(upd.subprocess, "run", lambda argv, **k: R() if argv[0] == "wslpath" else pytest.fail(argv))
    cmd = upd.git_pull_command(Path("/mnt/c/Users/someone/k3code/k3code"))
    assert cmd == [
        "/mnt/c/Program Files/Git/cmd/git.exe",
        "-C",
        "C:\\Users\\someone\\k3code\\k3code",
        "pull",
        "--ff-only",
    ]


def test_other_checkouts_are_pulled_with_the_plain_git(monkeypatch):
    monkeypatch.setattr(upd.shutil, "which", lambda name: "/x/" + name)
    monkeypatch.setattr(upd, "_in_wsl", lambda: True)
    assert upd.git_pull_command(Path("/home/u/src/k3code")) == ["git", "-C", "/home/u/src/k3code", "pull", "--ff-only"]
    monkeypatch.setattr(upd, "_in_wsl", lambda: False)  # not WSL: /mnt/c is just a path
    assert upd.git_pull_command(Path("/mnt/c/x"))[0] == "git"
    monkeypatch.setattr(upd, "_in_wsl", lambda: True)
    monkeypatch.setattr(upd.shutil, "which", lambda name: None)  # WSL without interop: nothing better to use
    assert upd.git_pull_command(Path("/mnt/c/x"))[0] == "git"


def test_an_authentication_failure_says_what_to_do(checkout, monkeypatch):
    class R:
        returncode = 128
        stdout = ""
        stderr = "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"

    monkeypatch.setattr(upd.subprocess, "run", lambda argv, **k: R())
    monkeypatch.setattr(upd, "_in_wsl", lambda: False)
    with pytest.raises(upd.SourceUpdateError) as exc:
        upd.pull_checkout(checkout)
    msg = str(exc.value)
    assert "git pull" in msg and "k3code update --from-source --no-pull" in msg and "terminal prompts disabled" in msg


def test_the_update_command_shows_that_advice_not_a_traceback(checkout, monkeypatch):
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", _deny)

    def pull_fails(src, *, pull=True):
        raise upd.SourceUpdateError("git could not reach GitHub from here")

    monkeypatch.setattr(upd, "update_from_source", pull_fails)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and "git could not reach GitHub" in r.output and "Traceback" not in r.output


async def test_the_slash_command_offers_the_checkout_too(checkout, monkeypatch):
    from k3code.commands.update_cmd import UpdateCommand

    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", _deny)
    monkeypatch.setattr(upd, "apply_detached", lambda: "Update started in the background (test).")
    out = await UpdateCommand().handle(None, None, "")
    assert f"built from {checkout}" in out["output"] and "/update now" in out["output"]
    out = await UpdateCommand().handle(None, None, "now")
    assert "Update started" in out["output"]


# -- the slash command on a git install (a version dir with `.ref`, no checkout, no releases) --------------------

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def git_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An install built from `main` at OLD[:7]; no release, no checkout; `apply_detached` is recorded."""

    def make(ref: str = "main") -> list[str]:
        data = tmp_path / "data"
        vdir = data / "versions" / f"0.1.0-src.{OLD[:7]}"
        vdir.mkdir(parents=True)
        (vdir / ".ref").write_text(ref + "\n")
        (data / "current").symlink_to(vdir)
        return applied

    applied: list[str] = []
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: None)
    monkeypatch.setattr(upd, "apply_detached", lambda: applied.append("applied") or "Update started (test).")
    return make


async def _slash(arg: str) -> str:
    from k3code.commands.update_cmd import UpdateCommand

    return (await UpdateCommand().handle(None, None, arg))["output"]


async def test_slash_update_on_an_up_to_date_git_install_says_so(git_install, monkeypatch):
    applied = git_install()
    monkeypatch.setattr(upd, "remote_head", lambda url, ref: OLD)
    for arg in ("", "now"):
        out = await _slash(arg)
        assert "No releases" not in out and "current: 0.1.0-src.aaaaaaa" in out
        assert f"latest:  {OLD[:7]} (main)" in out and "Already up to date." in out
    assert applied == []


async def test_slash_update_on_a_git_install_with_a_newer_head_offers_and_applies_it(git_install, monkeypatch):
    applied = git_install()
    monkeypatch.setattr(upd, "remote_head", lambda url, ref: NEW)
    out = await _slash("")
    assert f"latest:  {NEW[:7]} (main)" in out and "/update now" in out and "Already up to date" not in out
    assert applied == []
    out = await _slash("now")
    assert "Update started (test)." in out and applied == ["applied"]


async def test_slash_update_on_a_git_install_shows_a_remote_error_instead_of_raising(git_install, monkeypatch):
    applied = git_install()

    def boom(url, ref):
        raise upd.SourceUpdateError("git ls-remote x: no network (offline)")

    monkeypatch.setattr(upd, "remote_head", boom)
    for arg in ("", "now"):
        out = await _slash(arg)
        assert "Could not check main of" in out and "no network (offline)" in out
    assert applied == []


async def test_slash_update_on_a_git_install_survives_a_private_repo_release_lookup(git_install, monkeypatch):
    git_install()
    monkeypatch.setattr(upd, "fetch_latest", _deny)
    monkeypatch.setattr(upd, "remote_head", lambda url, ref: NEW)
    assert f"latest:  {NEW[:7]} (main)" in await _slash("")


async def test_slash_update_on_an_install_pinned_to_a_sha_has_nothing_to_update(git_install, monkeypatch):
    applied = git_install(ref=OLD)
    monkeypatch.setattr(upd, "remote_head", lambda url, ref: ref)
    for arg in ("", "now"):
        assert "pinned to commit" in await _slash(arg)
    assert applied == []


async def test_slash_update_without_release_checkout_or_git_ref_still_says_no_releases(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: None)
    assert "No releases on channel" in await _slash("")


async def test_slash_update_with_a_release_still_compares_versions(git_install, monkeypatch):
    applied = git_install()  # a `.ref` must not shadow a published release
    rel = upd.Release(tag="v9.9.9", version="9.9.9", body="notes", prerelease=False, assets={})
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: rel)
    monkeypatch.setattr(upd, "remote_head", lambda *a: pytest.fail("a release wins over the git ref"))
    out = await _slash("")
    assert "latest:  9.9.9" in out and "notes" in out
    assert "Update started" in await _slash("now") and applied == ["applied"]
