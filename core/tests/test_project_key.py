"""A project's state directory names the project: no two projects share one, one project never splits in two."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from k3code import mcpjson, paths
from k3code.config import McpServerConfig
from k3code.learning import projectstate
from k3code.memory import learned_notes_path, read_learned
from k3code.paths import project_key, project_state_dir


def _projects() -> Path:
    return paths.home() / "projects"


def _legacy(root: str | Path) -> Path:
    """The directory older versions kept a project's state in: its path with the separators flattened."""
    import re

    return _projects() / re.sub(r"[/:\\]+", "_", str(root))


def _seed(root: Path, **files: str) -> Path:
    legacy = _legacy(root)
    legacy.mkdir(parents=True, mode=0o700)
    for name, text in files.items():
        (legacy / name).write_text(text, encoding="utf-8")
    return legacy


def test_paths_that_flatten_to_one_string_are_two_projects() -> None:
    a, b = "/home/u/my_app/src", "/home/u/my/app_src"
    assert project_key(a) != project_key(b)
    assert project_state_dir(a) != project_state_dir(b)


def test_a_key_is_one_safe_directory_name() -> None:
    key = project_key("/home/u/my app/we:ird\\name")
    assert key == Path(key).name
    assert key.isascii() and not set(key) & set("/\\: ")
    assert project_key("/") != project_key("/home")  # the root has no name to show


def test_the_same_project_is_the_same_key_however_it_is_spelled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "link").symlink_to(repo)
    assert project_key(tmp_path / "link") == project_key(repo)
    assert project_key(repo / "sub" / "..") == project_key(repo)


def test_case_variants_of_a_windows_drive_path_are_one_project_under_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "_in_wsl", lambda: True)
    upper, lower = "/mnt/c/Users/me/orca/K3CODE", "/mnt/c/Users/me/orca/k3code"
    assert project_key(upper) == project_key(lower)
    assert project_key("/mnt/c/Users/me/orca/k3code2") != project_key(lower)
    assert project_key("/mnt/d/Users/me/orca/k3code") != project_key(lower)


def test_case_matters_where_the_file_system_tells_cases_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "_in_wsl", lambda: True)
    assert project_key("/home/me/Orca") != project_key("/home/me/orca")  # not a Windows drive
    monkeypatch.setattr(paths, "_in_wsl", lambda: False)
    assert project_key("/mnt/c/Users/me/K3CODE") != project_key("/mnt/c/Users/me/k3code")  # a plain Linux mount


def test_state_kept_under_the_old_name_is_copied_to_the_new_one(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    legacy = _seed(
        repo.resolve(),
        **{
            "learned.md": "# Learned\n\n- tests run with pytest\n",
            "project.json": '{"fingerprint": "abc", "version": 2}\n',
            mcpjson.STATE_FILE: '{"enabled": {"docs": "digest"}}\n',
            ".input_history": "+hello\n",
        },
    )
    (legacy / "learned.md").chmod(0o600)

    assert read_learned(repo / "sub") == ["tests run with pytest"]
    assert projectstate.load(repo)["fingerprint"] == "abc"
    assert mcpjson._load_state(repo) == {"docs": "digest"}

    new = learned_notes_path(repo).parent
    assert new != legacy
    assert stat.S_IMODE(new.stat().st_mode) == 0o700
    assert stat.S_IMODE((new / "learned.md").stat().st_mode) == 0o600
    assert not (new / ".input_history").exists()  # the TUI keeps its own history where it was
    assert (legacy / "learned.md").read_text(encoding="utf-8") == "# Learned\n\n- tests run with pytest\n"
    assert (legacy / ".input_history").is_file()  # the old directory is left as it was


def test_a_migrated_directory_is_not_overwritten_by_the_old_one(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    legacy = _seed(repo.resolve(), **{"learned.md": "# Learned\n\n- old note\n"})
    assert read_learned(repo) == ["old note"]
    learned_notes_path(repo).write_text("# Learned\n\n- new note\n", encoding="utf-8")
    (legacy / "learned.md").write_text("# Learned\n\n- older note\n", encoding="utf-8")
    assert read_learned(repo) == ["new note"]


def test_two_projects_that_shared_one_old_directory_each_keep_a_copy(tmp_path: Path) -> None:
    a, b = tmp_path / "my_app" / "src", tmp_path / "my" / "app_src"  # the same flattened name
    for repo in (a, b):
        repo.mkdir(parents=True)
    assert _legacy(a.resolve()) == _legacy(b.resolve())
    _seed(a.resolve(), **{"learned.md": "# Learned\n\n- shared note\n"})

    assert read_learned(a) == ["shared note"]
    assert read_learned(b) == ["shared note"]
    learned_notes_path(a).write_text("# Learned\n\n- only in a\n", encoding="utf-8")
    assert read_learned(a) == ["only in a"]
    assert read_learned(b) == ["shared note"]


def test_a_project_with_no_old_state_gets_no_directory_until_it_writes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert read_learned(repo) == []
    assert not learned_notes_path(repo).parent.exists()


def test_an_mcp_choice_in_one_project_does_not_reach_a_lookalike(tmp_path: Path) -> None:
    a, b = tmp_path / "x_y" / "z", tmp_path / "x" / "y_z"  # the same flattened name
    for repo in (a, b):
        repo.mkdir(parents=True)
    cfg = McpServerConfig(command="echo")
    mcpjson.set_enabled(a, "docs", cfg)
    assert mcpjson._load_state(a) == {"docs": mcpjson.digest(cfg)}
    assert mcpjson._load_state(b) == {}


def test_a_failed_move_leaves_no_half_migrated_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo.resolve(), **{"learned.md": "# Learned\n\n- old note\n"})

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only file system")

    with monkeypatch.context() as m:
        m.setattr(os, "rename", boom)
        assert read_learned(repo) == []
    assert [p.name for p in _projects().iterdir()] == [_legacy(repo.resolve()).name]  # no copy left behind
    assert read_learned(repo) == ["old note"]  # the next call tries again
