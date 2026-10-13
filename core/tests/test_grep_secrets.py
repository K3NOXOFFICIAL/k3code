"""The grep tool never returns lines from credential files, whatever its include glob (issue #138).

An include glob such as ``*`` or ``.env*`` turns ripgrep's hidden-file filter off, and rg lets the last matching glob
win, so ``include: "*"`` used to return ``.env`` and ``node_modules`` lines with no prompt in any mode.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from k3code import tools
from k3code.tools import tool_grep

#: built at run time so the secret scan never sees a key-shaped literal
FAKE = "API" + "_KEY=" + "not-a-real-value"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    proj = tmp_path / "proj"
    (proj / "node_modules").mkdir(parents=True)
    (proj / "sub").mkdir()
    (proj / ".env").write_text(FAKE + "\n")
    (proj / "sub" / ".env.local").write_text(FAKE + "\n")
    (proj / ".env.example").write_text("API_KEY=\n")
    (proj / "app.py").write_text("API_KEY = read_it()\n")
    (proj / "node_modules" / "a.js").write_text("API_KEY in a dependency\n")
    return proj


def _files(res: dict) -> set[str]:
    """The file names in grep's grouped output: a file's path on a line of its own, then its ``line:text`` matches."""
    return {Path(line).name for line in res["matches"].splitlines() if line and not re.match(r"\d+:", line)}


@pytest.mark.parametrize("include", ["*", ".env*", ["*.py", ".env"], "**/.env*"])
async def test_rg_include_globs_do_not_reach_credential_files(project: Path, include) -> None:
    if not shutil.which("rg"):
        pytest.skip("rg not installed")
    res = await tool_grep({"pattern": "API_KEY", "include": include}, cwd=project)
    assert "not-a-real-value" not in res["matches"]
    assert ".env" not in _files(res) and ".env.local" not in _files(res)
    assert "a.js" not in _files(res)  # the dependency excludes come after the include glob, so they win


@pytest.mark.parametrize("include", ["*", ".env*", ["*.py", ".env"], "**/.env*"])
async def test_rg_result_filter_alone_withholds_credential_lines_and_says_how_many(
    project: Path, monkeypatch: pytest.MonkeyPatch, include
) -> None:
    """rg's own credential excludes (grep_excludes) normally keep these files from being read at all; with them
    gone the filter on rg's output is what drops the lines, and it names how many it withheld."""
    if not shutil.which("rg"):
        pytest.skip("rg not installed")
    monkeypatch.setattr(tools, "grep_excludes", lambda base: [])
    res = await tool_grep({"pattern": "API_KEY", "include": include}, cwd=project)
    assert "not-a-real-value" not in res["matches"]
    assert ".env" not in _files(res) and ".env.local" not in _files(res)
    assert "not shown" in str(res.get("warnings"))


async def test_rg_include_keeps_code_and_a_template_is_searched_by_naming_it(project: Path) -> None:
    if not shutil.which("rg"):
        pytest.skip("rg not installed")
    res = await tool_grep({"pattern": "API_KEY", "include": "*"}, cwd=project)
    # rg's credential excludes keep every .env.* out of a directory search, templates too (stricter than
    # sensitive_path, which the Python fallback applies: it still finds the template)
    assert "app.py" in _files(res) and ".env.example" not in _files(res)
    named = await tool_grep({"pattern": "API_KEY", "path": ".env.example"}, cwd=project)
    assert "API_KEY=" in named["matches"]


async def test_rg_plain_search_is_unchanged(project: Path) -> None:
    if not shutil.which("rg"):
        pytest.skip("rg not installed")
    res = await tool_grep({"pattern": "API_KEY"}, cwd=project)
    assert _files(res) == {"app.py"}
    assert res["matches"] == "app.py\n1:API_KEY = read_it()"  # the path (relative to the session) once, then its lines
    assert "warnings" not in res


@pytest.mark.parametrize("include", [None, "*", ".env*"])
async def test_the_python_fallback_skips_credential_files(
    project: Path, monkeypatch: pytest.MonkeyPatch, include
) -> None:
    monkeypatch.setenv("PATH", str(project / "no-bin"))  # no rg, no git: the Python walk, which lists hidden files
    args = {"pattern": "API_KEY", **({"include": include} if include else {})}
    res = await tool_grep(args, cwd=project)
    assert "not-a-real-value" not in res["matches"]
    assert ".env" not in _files(res) and ".env.local" not in _files(res)
    assert "not searched" in str(res.get("warnings"))
    if include != ".env*":
        assert "app.py" in _files(res)
