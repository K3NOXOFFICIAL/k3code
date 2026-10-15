"""Memory ``@import`` and credential stores of ``$HOME`` (K3NOX-713, follow-up to K3NOX-649).

Memory files are model-writable (``~/AGENTS.md`` in a session rooted at ``$HOME``, or any ordinary home file the
user's ``USER.md`` imports) and the import path has no ``decide()``, so the loader itself must refuse a credential
location however it is reached: through a link out of a store, with ``$K3CODE_HOME`` at or above a store, nested under
``USER.md``, or inside a ``$K3CODE_HOME`` that has no dot. Fake HOME (the conftest fixture) and fake values only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from k3code.memory import load_memory, scan_memory

TOKEN = "fake-" + "token-for-test"


def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _text(cwd: Path) -> str:
    return " ".join(m.text for m in load_memory(cwd))


def _k3home(monkeypatch: pytest.MonkeyPatch, rel: str = ".k3code") -> Path:
    path = Path.home() / rel
    monkeypatch.setenv("K3CODE_HOME", str(path))
    return path


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def test_a_store_file_that_is_a_link_out_of_the_store_is_refused_from_a_home_rooted_project(monkeypatch) -> None:
    """A dotfiles manager (stow, a hand-made repo) leaves ``~/.config/gh/hosts.yml`` as a link to a file outside the
    store: the path as written is the credential, whatever it resolves to."""
    home = Path.home()
    _k3home(monkeypatch)
    real = _w(home / "dotfiles" / "gh" / "hosts.yml", "oauth_token: " + TOKEN)
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").symlink_to(real)
    _w(home / "AGENTS.md", "@.config/gh/hosts.yml")
    assert TOKEN not in _text(home)


def test_a_store_file_that_is_a_link_out_of_the_store_is_refused_from_user_memory(monkeypatch) -> None:
    home = Path.home()
    k3 = _k3home(monkeypatch)
    real = _w(home / "dotfiles" / "docker" / "config.json", '{"auth": "' + TOKEN + '"}')
    (home / ".docker").mkdir(parents=True)
    (home / ".docker" / "config.json").symlink_to(real)
    _w(k3 / "memory" / "USER.md", "@~/.docker/config.json")
    assert TOKEN not in _text(home / "nowhere")


def test_a_link_chain_through_a_store_is_refused_at_every_hop(monkeypatch) -> None:
    """An ordinary-looking file links to a store file that links on to a file outside it: the middle hop counts."""
    home = Path.home()
    _k3home(monkeypatch)
    real = _w(home / "dotfiles" / "gh" / "hosts.yml", "oauth_token: " + TOKEN)
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").symlink_to(real)
    (home / "notes.md").symlink_to(home / ".config" / "gh" / "hosts.yml")
    _w(home / "AGENTS.md", "@notes.md")
    assert TOKEN not in _text(home)


def test_a_store_that_is_a_link_to_a_directory_outside_it_stays_refused(monkeypatch) -> None:
    home = Path.home()
    _k3home(monkeypatch)
    _w(home / "dotfiles" / "gh" / "hosts.yml", "oauth_token: " + TOKEN)
    (home / ".config").mkdir()
    (home / ".config" / "gh").symlink_to(home / "dotfiles" / "gh")
    _w(home / "AGENTS.md", "@.config/gh/hosts.yml\n@dotfiles/gh/hosts.yml")
    assert TOKEN not in _text(home)


def test_k3code_home_at_home_does_not_make_the_stores_importable(monkeypatch) -> None:
    home = Path.home()
    monkeypatch.setenv("K3CODE_HOME", str(home))
    _w(home / ".npmrc", "//registry/:_authToken=" + TOKEN)
    _w(home / "AGENTS.md", "@.npmrc")
    assert TOKEN not in _text(home)


def test_k3code_home_above_a_store_does_not_make_it_importable_from_user_memory(monkeypatch) -> None:
    home = Path.home()
    _k3home(monkeypatch, ".config")
    _w(home / ".config" / "gh" / "hosts.yml", "oauth_token: " + TOKEN)
    _w(home / ".config" / "memory" / "USER.md", "@~/.config/gh/hosts.yml")
    assert TOKEN not in _text(home / "nowhere")


def test_user_memory_still_reads_its_own_folder_when_k3code_home_sits_in_a_store_folder(monkeypatch) -> None:
    home = Path.home()
    _k3home(monkeypatch, ".config")
    _w(home / ".config" / "memory" / "other.md", "OWN-STATE")
    _w(home / ".config" / "memory" / "USER.md", "@other.md")
    assert "OWN-STATE" in _text(home / "nowhere")


def test_an_import_nested_under_user_memory_gets_the_dot_folder_rule(monkeypatch) -> None:
    """``USER.md`` imports an ordinary home file; a session rooted at ``$HOME`` can rewrite that file to point at a
    credential file that is in no store list."""
    home = Path.home()
    k3 = _k3home(monkeypatch)
    _w(home / ".config" / "git" / "credentials", "https://u:" + TOKEN + "@h")
    _w(home / "notes.md", "@.config/git/credentials")
    _w(k3 / "memory" / "USER.md", "@~/notes.md")
    assert TOKEN not in _text(home / "nowhere")


def test_user_memory_cannot_import_a_dot_folder_credential_it_names_itself(monkeypatch) -> None:
    """The model in a session rooted at ``$HOME`` may write ``USER.md`` without a prompt, so a direct import of a dot
    folder from it is as untrusted as a nested one."""
    home = Path.home()
    k3 = _k3home(monkeypatch)
    _w(home / ".config" / "git" / "credentials", "https://u:" + TOKEN + "@h")
    _w(k3 / "memory" / "USER.md", "@~/.config/git/credentials")
    assert TOKEN not in _text(home / "nowhere")
    assert TOKEN not in _text(_repo(home / "src" / "proj"))


def test_a_state_folder_without_a_dot_is_not_importable_from_a_home_rooted_project(monkeypatch) -> None:
    """``K3CODE_HOME`` holds the session transcripts and journals; the sandbox hides it wherever it is."""
    home = Path.home()
    k3 = _k3home(monkeypatch, "k3state")
    _w(k3 / "sessions" / "s.jsonl", TOKEN)
    _w(home / "AGENTS.md", "@k3state/sessions/s.jsonl")
    assert TOKEN not in _text(home)


def test_a_state_folder_outside_home_is_not_importable_from_user_memory(monkeypatch, tmp_path: Path) -> None:
    k3 = tmp_path / "state"
    monkeypatch.setenv("K3CODE_HOME", str(k3))
    _w(k3 / "sessions" / "s.jsonl", TOKEN)
    _w(k3 / "memory" / "USER.md", "@" + str(k3 / "sessions" / "s.jsonl"))
    assert TOKEN not in _text(Path.home() / "nowhere")


def test_a_project_inside_a_store_keeps_its_own_memory(monkeypatch) -> None:
    """A root inside a store makes its own files ordinary, as for the in-process readers (engine._home_store_read)."""
    home = Path.home()
    _k3home(monkeypatch)
    root = _repo(home / ".claude" / "skills" / "myskill")
    _w(root / "CLAUDE.md", "@rules.md")
    _w(root / "rules.md", "SKILL-RULES")
    assert "SKILL-RULES" in _text(root)
    assert all(s.status != "refused" for s in scan_memory(root) if s.path.name == "CLAUDE.md")


def test_a_project_inside_a_state_folder_keeps_its_own_memory(monkeypatch) -> None:
    k3 = _k3home(monkeypatch)
    root = _repo(k3 / "worktrees" / "proj")
    _w(root / "AGENTS.md", "PROJECT-RULES")
    assert "PROJECT-RULES" in _text(root)


def test_a_repository_below_home_with_k3code_home_at_home_keeps_its_memory(monkeypatch) -> None:
    home = Path.home()
    monkeypatch.setenv("K3CODE_HOME", str(home))
    root = _repo(home / "src" / "proj")
    _w(root / ".github" / "contributing.md", "CI-RULES")
    _w(root / "AGENTS.md", "@.github/contributing.md")
    assert "CI-RULES" in _text(root)


def test_user_memory_keeps_ordinary_home_files_and_its_own_folder(monkeypatch) -> None:
    home = Path.home()
    k3 = _k3home(monkeypatch)
    _w(home / "notes" / "prefs.md", "MY-PREFS")
    _w(k3 / "memory" / "extra.md", "K3-EXTRA")
    _w(k3 / "memory" / "USER.md", "@~/notes/prefs.md\n@extra.md")
    text = _text(home / "nowhere")
    assert "MY-PREFS" in text and "K3-EXTRA" in text


def test_the_intended_refusal_of_claude_code_memory_stays(monkeypatch) -> None:
    """``~/.claude`` holds Claude Code's login: not importable from user memory (K3NOX decides that, not this fix)."""
    home = Path.home()
    k3 = _k3home(monkeypatch)
    _w(home / ".claude" / "CLAUDE.md", "CC-PREFS")
    _w(k3 / "memory" / "USER.md", "@~/.claude/CLAUDE.md")
    assert "CC-PREFS" not in _text(home / "nowhere")


def test_a_home_that_is_itself_a_link_keeps_the_stores_refused(monkeypatch, tmp_path: Path) -> None:
    """``/home/u -> /var/home/u``: the stores are named by either spelling of ``$HOME``."""
    real = tmp_path / "var" / "home" / "u"
    real.mkdir(parents=True)
    link = tmp_path / "home-u"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(link))
    monkeypatch.setenv("K3CODE_HOME", str(link / ".k3code"))
    _w(real / ".npmrc", "//registry/:_authToken=" + TOKEN)
    _w(real / ".config" / "git" / "credentials", "https://u:" + TOKEN + "@h")
    _w(real / "AGENTS.md", "@.npmrc\n@.config/git/credentials\n@" + str(real / ".npmrc"))
    assert TOKEN not in _text(link)
    assert TOKEN not in _text(real)


def test_a_project_root_above_home_refuses_the_stores_and_dot_folders(monkeypatch, tmp_path: Path) -> None:
    users = tmp_path / "users"  # a repository of its own above the fake home: never the shared basetemp
    home = users / "u"
    home.mkdir(parents=True)
    _repo(users)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("K3CODE_HOME", str(home / ".k3code"))
    _w(home / ".npmrc", "//registry/:_authToken=" + TOKEN)
    _w(home / ".config" / "git" / "credentials", "https://u:" + TOKEN + "@h")
    _w(home / "AGENTS.md", "@.npmrc\n@.config/git/credentials")
    assert TOKEN not in _text(home)


def test_a_proc_root_spelling_of_a_store_is_refused(monkeypatch) -> None:
    home = Path.home()
    _k3home(monkeypatch)
    _w(home / ".npmrc", "//registry/:_authToken=" + TOKEN)
    _w(home / "AGENTS.md", f"@/proc/self/root{home}/.npmrc")
    assert TOKEN not in _text(home)


def test_odd_import_targets_do_not_raise(monkeypatch) -> None:
    home = Path.home()
    _k3home(monkeypatch)
    home.mkdir(parents=True, exist_ok=True)
    os.mkfifo(home / "fifo")
    _w(home / "AGENTS.md", "@fifo\n@/dev/null\n@/proc/self/environ\n@" + "a" * 300 + "\n@.\n@loop")
    (home / "loop").symlink_to(home / "loop")
    assert "fifo" in _text(home)


def test_a_dotdot_after_a_link_is_followed_on_disk_not_by_name(monkeypatch) -> None:
    """``sub`` links into ``dotfiles/deep``: ``sub/../x`` is ``dotfiles/x`` there, not ``x`` next to ``sub``."""
    home = Path.home()
    _k3home(monkeypatch)
    _w(home / "dotfiles" / "x.md", "IN-DOTFILES")
    _w(home / "x.md", "BESIDE-SUB")
    (home / "dotfiles" / "deep").mkdir()
    (home / "sub").symlink_to(home / "dotfiles" / "deep")
    _w(home / "AGENTS.md", "@sub/../x.md")
    assert _text(home) == "IN-DOTFILES"


# User memory kept in a dotfiles repository through links (stow, a hand-made repo) is ordinary and must keep working:
# the memory folder is its own place both where it sits and where it leads.


def test_user_memory_file_that_is_a_link_into_a_dotfiles_repository_is_read(monkeypatch) -> None:
    home = Path.home()
    k3 = _k3home(monkeypatch)
    real = _w(home / "dotfiles" / "k3code" / "USER.md", "MY-PREFS")
    (k3 / "memory").mkdir(parents=True)
    (k3 / "memory" / "USER.md").symlink_to(real)
    assert "MY-PREFS" in _text(home / "nowhere")
    assert next(s for s in scan_memory(home / "nowhere") if s.scope == "user").status == "read"


def test_user_memory_folder_that_is_a_link_into_a_dotfiles_repository_is_read(monkeypatch) -> None:
    home = Path.home()
    k3 = _k3home(monkeypatch)
    _w(home / "dotfiles" / "k3code" / "memory" / "USER.md", "@style.md")
    _w(home / "dotfiles" / "k3code" / "memory" / "style.md", "MY-STYLE")
    k3.mkdir(parents=True)
    (k3 / "memory").symlink_to(home / "dotfiles" / "k3code" / "memory")
    assert "MY-STYLE" in _text(home / "nowhere")


def test_user_memory_import_that_is_a_link_into_a_dotfiles_repository_is_read(monkeypatch) -> None:
    home = Path.home()
    k3 = _k3home(monkeypatch)
    real = _w(home / "dotfiles" / "style.md", "MY-STYLE")
    _w(k3 / "memory" / "USER.md", "@style.md")
    (k3 / "memory" / "style.md").symlink_to(real)
    assert "MY-STYLE" in _text(home / "nowhere")


def test_a_link_from_the_memory_folder_to_a_credential_file_is_still_refused(monkeypatch) -> None:
    """The memory folder is exempt for what sits in it, not for where a link in it leads."""
    home = Path.home()
    k3 = _k3home(monkeypatch)
    _w(home / ".config" / "git" / "credentials", "https://u:" + TOKEN + "@h")
    _w(home / ".config" / "gh" / "hosts.yml", "oauth_token: " + TOKEN)
    (k3 / "memory").mkdir(parents=True)
    (k3 / "memory" / "a.md").symlink_to(home / ".config" / "git" / "credentials")
    (k3 / "memory" / "b.md").symlink_to(home / ".config" / "gh" / "hosts.yml")
    (k3 / "memory" / "ghdir").symlink_to(home / ".config" / "gh")
    _w(k3 / "memory" / "USER.md", "@a.md\n@b.md\n@ghdir/hosts.yml")
    assert TOKEN not in _text(home / "nowhere")
