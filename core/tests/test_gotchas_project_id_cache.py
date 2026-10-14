"""gotchas_prompt runs on every prompt build (every turn, every token estimate): no ``git config`` call each time."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from k3code.learning import decisions, gotchas

GIT_ID = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _repo(path: Path, remote: str) -> Path:
    _git("init", "-q", str(path))
    _git("-C", str(path), "remote", "add", "origin", remote)
    return path


def _accept(home: Path, pid: str, line: str) -> None:
    gotchas.append_gotcha(gotchas.gotchas_path(pid, home), line)


def _count_git_calls(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(decisions.subprocess, "run", spy)
    return calls


def _bump(path: Path) -> None:
    """A filesystem with coarse timestamps could leave a rewrite with the old mtime: move it on."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_repeated_prompt_builds_spawn_one_git_subprocess(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = _repo(tmp_path / "app", "https://github.com/acme/app.git")
    _accept(home, "git:github.com/acme/app", "npm test failed; `pnpm test` worked instead")
    calls = _count_git_calls(monkeypatch)

    blocks = [gotchas.gotchas_prompt(repo, home) for _ in range(5)]

    assert all("pnpm test" in b for b in blocks) and len(set(blocks)) == 1
    assert len(calls) <= 1, calls


def test_a_changed_remote_is_seen_on_the_next_build(tmp_path):
    home = tmp_path / "home"
    repo = _repo(tmp_path / "app", "https://github.com/acme/app.git")
    _accept(home, "git:github.com/acme/app", "line for app")
    _accept(home, "git:github.com/acme/other", "line for other")
    assert "line for app" in gotchas.gotchas_prompt(repo, home)

    _git("-C", str(repo), "remote", "set-url", "origin", "https://github.com/acme/other.git")
    _bump(repo / ".git" / "config")

    block = gotchas.gotchas_prompt(repo, home)
    assert "line for other" in block and "line for app" not in block


def test_a_remote_added_after_the_first_build_is_seen(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "app"
    _git("init", "-q", str(repo))
    _accept(home, "git:github.com/acme/app", "line for app")
    assert gotchas.gotchas_prompt(repo, home) == ""  # no remote: a path id, no gotchas

    _git("-C", str(repo), "remote", "add", "origin", "https://github.com/acme/app.git")
    _bump(repo / ".git" / "config")

    assert "line for app" in gotchas.gotchas_prompt(repo, home)


def test_a_linked_worktree_follows_the_main_repos_config(tmp_path, monkeypatch):
    # fan-out workers run in worktrees: their .git is a file, the remote lives in the main repo's config
    home = tmp_path / "home"
    repo = _repo(tmp_path / "app", "https://github.com/acme/app.git")
    _git("-C", str(repo), *GIT_ID, "commit", "-q", "--allow-empty", "-m", "init")
    wt = tmp_path / "wt"
    _git("-C", str(repo), "worktree", "add", "-q", str(wt))
    assert (wt / ".git").is_file()
    _accept(home, "git:github.com/acme/app", "line for app")
    _accept(home, "git:github.com/acme/other", "line for other")
    calls = _count_git_calls(monkeypatch)

    assert "line for app" in gotchas.gotchas_prompt(wt, home)
    assert "line for app" in gotchas.gotchas_prompt(wt / ".", home)
    assert len(calls) <= 1, calls

    _git("-C", str(repo), "remote", "set-url", "origin", "https://github.com/acme/other.git")
    _bump(repo / ".git" / "config")
    assert "line for other" in gotchas.gotchas_prompt(wt, home)


def test_a_directory_outside_any_repo_is_cached_too(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    calls = _count_git_calls(monkeypatch)

    for _ in range(4):
        assert gotchas.gotchas_prompt(plain, home) == ""

    assert len(calls) <= 1, calls
