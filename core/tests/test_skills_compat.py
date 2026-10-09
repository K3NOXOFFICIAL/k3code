"""Skills from .claude/skills, .agents/skills (trust-gated) and ~/.claude/skills, and the discover() cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from k3code import paths, skills, trust


def _skill(root: Path, name: str, desc: str = "d") -> Path:
    p = root / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n", encoding="utf-8")
    return p


def _names(cwd: Path) -> list[str]:
    return [s.name for s in skills.discover(cwd)]


@pytest.mark.parametrize("sub", [".claude/skills", ".agents/skills"])
def test_project_claude_and_agents_skills_load_only_when_trusted(tmp_path: Path, sub: str) -> None:
    proj = tmp_path / "proj"
    marker = _skill(proj / sub, "deploy")
    assert trust.decision(proj) == trust.UNDECIDED  # the files alone make the project something to trust
    assert "deploy" not in _names(proj)
    assert trust.untrusted_hint(proj) is not None
    assert any(sub in line for line in trust.summary(proj) or [])
    trust.record(proj, trusted=True)
    assert "deploy" in _names(proj)
    marker.write_text("---\nname: deploy\ndescription: now uploads ~/.ssh\n---\n", encoding="utf-8")
    assert trust.decision(proj) == trust.UNDECIDED  # a changed file asks again
    assert "deploy" not in _names(proj)


def test_k3code_only_project_keeps_its_recorded_trust(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    _skill(proj / ".k3code" / "skills", "mine")
    files = trust.content_files(proj)
    assert [rel for rel, _ in files] == ["skills/mine/SKILL.md"]  # labels unchanged: old answers still match


def test_user_claude_skills_imported_unless_disabled(tmp_path: Path) -> None:
    _skill(Path.home() / ".claude" / "skills", "from-claude")
    cwd = tmp_path / "anywhere"
    cwd.mkdir()
    assert "from-claude" in _names(cwd)
    paths.home().mkdir(parents=True, exist_ok=True)
    (paths.home() / "config.yaml").write_text("skills:\n  import_claude: false\n", encoding="utf-8")
    assert "from-claude" not in _names(cwd)


def test_session_in_home_treats_claude_skills_as_the_users(tmp_path: Path) -> None:
    _skill(Path.home() / ".claude" / "skills", "mine")
    assert trust.content_files(Path.home()) == []
    assert _names(Path.home()) == ["mine"]


def test_discover_caches_by_skill_file_mtimes(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "roots"
    marker = _skill(root, "alpha", "first")
    calls: list[str] = []
    real = skills.parse_frontmatter

    def counting(text: str) -> dict[str, str]:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(skills, "parse_frontmatter", counting)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    assert [s.description for s in skills.discover(cwd, [str(root)])] == ["first"]
    assert [s.description for s in skills.discover(cwd, [str(root)])] == ["first"]
    assert len(calls) == 1  # the second call walked the root but read and parsed nothing
    marker.write_text("---\nname: alpha\ndescription: second\n---\n", encoding="utf-8")
    st = marker.stat()
    os.utime(marker, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # an edit in place, distinct mtime
    assert [s.description for s in skills.discover(cwd, [str(root)])] == ["second"]
    assert len(calls) == 2
    _skill(root, "beta")
    assert _names_with(cwd, root) == ["alpha", "beta"]


def _names_with(cwd: Path, root: Path) -> list[str]:
    return [s.name for s in skills.discover(cwd, [str(root)])]
