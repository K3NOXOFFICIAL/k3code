"""grep include/exclude globs and bad patterns behave the same with and without ripgrep (#139)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from k3code.tools import tool_grep

FILES = ("tests/a.py", "tests/sub/b.py", "src/c.py", "other/tests/d.py", "top.py", "src/top.py", "notes.md")


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    root = tmp_path / "proj"
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("needle\n")
    # rg anchors a glob to its working directory: the daemon's is not the project's
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return root


@pytest.fixture
def no_rg(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))


def _hits(res: dict, root: Path) -> set[str]:
    return {Path(line.split(":")[0]).relative_to(root).as_posix() for line in res["matches"].splitlines()}


CASES = [
    # a glob without a slash matches the file name at any depth
    ({"include": "*.py"}, {"tests/a.py", "tests/sub/b.py", "src/c.py", "src/top.py", "other/tests/d.py", "top.py"}),
    ({"include": "top.py"}, {"top.py", "src/top.py"}),
    ({"include": "/top.py"}, {"top.py"}),
    # a glob with a slash is anchored at the searched directory
    ({"include": "tests/*.py"}, {"tests/a.py"}),
    ({"include": "/tests/*.py"}, {"tests/a.py"}),
    ({"include": "**/tests/*.py"}, {"tests/a.py", "other/tests/d.py"}),
    ({"include": "tests/**"}, {"tests/a.py", "tests/sub/b.py"}),
    ({"include": ["tests/*.py", "*.md"]}, {"tests/a.py", "notes.md"}),
    (
        {"include": "*.py", "exclude": "tests/*.py"},
        {"tests/sub/b.py", "src/c.py", "src/top.py", "other/tests/d.py", "top.py"},
    ),
    ({"include": "*.py", "exclude": "top.py"}, {"tests/a.py", "tests/sub/b.py", "src/c.py", "other/tests/d.py"}),
]


@pytest.mark.parametrize(("args", "expected"), CASES, ids=[str(a) for a, _ in CASES])
async def test_the_grep_fallback_matches_path_globs(project, no_rg, args, expected):
    res = await tool_grep({"pattern": "needle", **args}, cwd=project)
    assert _hits(res, project) == expected


@pytest.mark.parametrize(("args", "expected"), CASES, ids=[str(a) for a, _ in CASES])
async def test_grep_with_rg_matches_the_same_files(project, args, expected):
    if not shutil.which("rg"):
        pytest.skip("rg not installed")
    res = await tool_grep({"pattern": "needle", **args}, cwd=project)
    assert _hits(res, project) == expected


async def test_a_path_glob_is_relative_to_the_searched_directory(project, no_rg):
    res = await tool_grep({"pattern": "needle", "path": "other", "include": "tests/*.py"}, cwd=project)
    assert _hits(res, project) == {"other/tests/d.py"}


async def test_a_bad_pattern_reports_rgs_message(project):
    if not shutil.which("rg"):
        pytest.skip("rg not installed")
    with pytest.raises(RuntimeError, match="unclosed group"):
        await tool_grep({"pattern": "foo("}, cwd=project)


async def test_a_pattern_only_python_accepts_still_searches(project):
    if not shutil.which("rg"):
        pytest.skip("rg not installed")
    (project / "src" / "c.py").write_text("ab\nac\n")
    res = await tool_grep({"pattern": "a(?=b)", "path": "src"}, cwd=project)  # look-around: rg refuses, re accepts
    assert res["matches"].endswith("c.py:1:ab")


async def test_a_bad_pattern_without_rg_still_fails(project, no_rg):
    # an error naming the fault and how to escape it, not a raised re.error ("Tool execution failed: error: ...")
    res = await tool_grep({"pattern": "foo("}, cwd=project)
    assert "matches" not in res
    assert res["error"].startswith("invalid regex for 'pattern' ('foo('): missing ), unterminated subpattern")
    assert "escape metacharacters" in res["error"]
