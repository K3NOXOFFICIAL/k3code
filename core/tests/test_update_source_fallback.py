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
