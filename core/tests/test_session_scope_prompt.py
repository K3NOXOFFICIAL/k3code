"""The system prompt tells the model where its session works (the owner's "reads random things outside the project",
"no idea where to start"): before this, no section named the working directory, so the model searched ``~`` or ``/``
to orient itself and every read there asked or was refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code import prompting
from k3code.config import Settings


def _git_init(path: Path) -> None:
    (path / ".git").mkdir()  # all the scope section looks at (memory.project_root): no git run needed


def _section(text: str) -> str:
    start = text.index("## Session scope")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def test_the_prompt_names_the_working_directory(home: Path) -> None:
    proj = home / "src" / "app"
    proj.mkdir(parents=True)
    _git_init(proj)
    text = prompting.build_system_prompt(prompting.base_prompt(), cwd=proj, config=Settings())
    scope = _section(text)
    assert "Working directory: `~/src/app`" in scope
    assert "never from `~` or `/`" in scope
    assert "git repository" not in scope.lower()  # the cwd is the repository root: nothing more to say


def test_a_subdirectory_of_a_repo_names_the_root_as_outside_the_scope(home: Path) -> None:
    repo = home / "repo"
    sub = repo / "core"
    sub.mkdir(parents=True)
    _git_init(repo)
    scope = prompting.scope_prompt(sub)
    assert "Working directory: `~/repo/core`" in scope
    assert "Git repository root: `~/repo`" in scope and "outside this session's scope" in scope


def test_a_directory_outside_git_says_so(tmp_path: Path, home: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    scope = prompting.scope_prompt(plain)
    assert f"Working directory: `{plain.resolve().as_posix()}`" in scope  # outside home: the absolute path
    assert "Not inside a git repository." in scope


def test_add_dirs_from_the_session_are_in_scope(home: Path, tmp_path: Path) -> None:
    proj = home / "app"
    proj.mkdir()
    data = home / "data"
    data.mkdir()
    text = prompting.build_system_prompt(
        prompting.base_prompt(), cwd=proj, config=Settings(), session_meta={"add_dirs": [str(data)]}
    )
    assert "Also in scope (added with /add-dir): `~/data`" in _section(text)


@pytest.mark.parametrize("where", ["home", "root"])
def test_a_session_in_home_or_above_is_told_it_has_no_project(home: Path, where: str) -> None:
    cwd = home if where == "home" else Path("/")
    scope = prompting.scope_prompt(cwd)
    assert "not a project" in scope
    assert "never search, list or read the home directory recursively" in scope
    assert "ask which one" in scope


def test_the_section_is_stable_and_sits_before_the_project_facts_and_memory(home: Path) -> None:
    proj = home / "app"
    proj.mkdir()
    (proj / "K3CODE.md").write_text("Use make test.\n", encoding="utf-8")
    names = [n for n, _ in prompting.prompt_sections(prompting.base_prompt(), cwd=proj, config=Settings())]
    assert "scope" in names and "memory" in names
    assert names.index("scope") < names.index("memory")
    first = prompting.build_system_prompt(prompting.base_prompt(), cwd=proj, config=Settings())
    assert first == prompting.build_system_prompt(prompting.base_prompt(), cwd=proj, config=Settings())


def test_every_prompt_mode_carries_the_scope(home: Path) -> None:
    proj = home / "app"
    proj.mkdir()
    for mode in prompting.mode_names(proj):
        text, _sections = prompting.assemble_mode(mode, cwd=proj, config=Settings())
        assert "## Session scope" in text, mode
