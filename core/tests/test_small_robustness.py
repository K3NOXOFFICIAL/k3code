"""Small robustness fixes: skill-use outcomes, atomic .usage.json, mem0 posts once, glob/grep tree rules and caps,
blocking scans off the event loop."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from k3code import skills as skills_mod
from k3code.config import Mem0Config, Settings
from k3code.extratools import register_skill_tool
from k3code.learning import curator, distiller, projectprep
from k3code.tools import ToolRegistry, format_tool_result, tool_glob, tool_grep

#: tools.MAX_SEARCH_RESULTS (a literal, so the tests still collect with the fix set aside)
MAX_SEARCH_RESULTS = 1000


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "k3home"
    monkeypatch.setenv("K3CODE_HOME", str(h))
    return h


@pytest.fixture
def no_git_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)


def _skill_tool(tmp_path: Path):
    root = tmp_path / "skills-root"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: a demo\n---\nDo the demo.\n")
    reg = ToolRegistry()
    register_skill_tool(reg, tmp_path, [str(root)])
    return reg.get("skill")[1]


async def test_a_skill_that_cannot_be_read_counts_as_a_failed_use(tmp_path, home, monkeypatch):
    handler = _skill_tool(tmp_path)
    assert "Do the demo." in (await handler({"name": "demo"}))["content"]

    def unreadable(self):
        raise OSError("gone")

    monkeypatch.setattr(skills_mod.Skill, "text", unreadable)
    res = await handler({"name": "demo"})
    assert "could not be read" in res["error"]
    usage = curator.load_usage()["demo"]
    assert usage == {"uses": 2, "failures": 1, "last_used": usage["last_used"]}


def test_skill_usage_is_written_atomically(home, monkeypatch):
    replaced: list[tuple[str, str]] = []
    real = os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    curator.record_use("demo", ok=False)
    path = home / "skills" / ".usage.json"
    assert [d for _, d in replaced] == [str(path)]
    assert curator.load_usage()["demo"]["failures"] == 1
    assert sorted(p.name for p in path.parent.iterdir()) == [".usage.json"]  # no temp file left


def test_mem0_posts_each_preference_once(tmp_path):
    cfg = Settings(mem0=Mem0Config(url="http://mem0.test", api_key_env="NOPE", agent_id="a"))
    sent: list[str] = []
    prefs = [distiller.Preference("prefers small commits", 0.8, 3), distiller.Preference("runs tests first", 0.7, 4)]
    posted = tmp_path / "learning" / "mem0_posted.json"

    def post(url, body, headers):
        sent.append(body["messages"][0]["content"])

    assert distiller.store_mem0(cfg, prefs, post, posted) == 2
    assert distiller.store_mem0(cfg, prefs, post, posted) == 0  # the next daily distill: nothing new
    more = [*prefs, distiller.Preference("uses uv", 0.9, 5)]
    assert distiller.store_mem0(cfg, more, post, posted) == 1
    assert len(sent) == 3
    other = Settings(mem0=Mem0Config(url="http://mem0.test", api_key_env="NOPE", agent_id="b"))
    assert distiller.store_mem0(other, prefs, post, posted) == 2  # another agent id is another target


def test_a_failed_mem0_post_is_tried_again(tmp_path):
    cfg = Settings(mem0=Mem0Config(url="http://mem0.test", api_key_env="NOPE", agent_id="a"))
    posted = tmp_path / "mem0_posted.json"
    prefs = [distiller.Preference("prefers small commits", 0.8, 3)]
    assert distiller.store_mem0(cfg, prefs, lambda *a: 1 / 0, posted) == 0
    assert distiller.store_mem0(cfg, prefs, lambda *a: None, posted) == 1


def _tree(root: Path) -> None:
    for rel in (
        "src/a.py",
        "src/pkg/b.py",
        "node_modules/x/c.py",
        ".venv/lib/d.py",
        "dist/e.py",
        ".git/hooks/f.py",
        "build/g.py",
        "README.md",
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("needle\n")


async def test_glob_skips_vendor_dirs_without_git(tmp_path, no_git_env):
    _tree(tmp_path)
    res = await tool_glob({"pattern": "*.py"}, cwd=tmp_path)
    assert res["files"] == ["build/g.py", "src/a.py", "src/pkg/b.py"]
    assert (await tool_glob({"pattern": "src/*.py"}, cwd=tmp_path))["files"] == ["src/a.py"]
    assert (await tool_glob({"pattern": "**/pkg"}, cwd=tmp_path))["files"] == ["src/pkg"]


async def test_glob_honours_gitignore_in_a_work_tree(tmp_path, no_git_env):
    assert shutil.which("git")
    _tree(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\nnode_modules/\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    assert (tmp_path / ".git" / "HEAD").is_file()  # the repository is the test's own
    res = await tool_glob({"pattern": "*.py"}, cwd=tmp_path)
    assert res["files"] == ["src/a.py", "src/pkg/b.py"]  # build/ ignored by .gitignore, the rest by SKIP_DIRS


async def test_glob_omits_files_deleted_from_the_work_tree(tmp_path, no_git_env):
    assert shutil.which("git")
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "sub" / "b.py").write_text("b\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    assert (tmp_path / ".git" / "HEAD").is_file()  # the repository is the test's own
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    (tmp_path / "a.py").unlink()  # deleted without staging: still in the index
    res = await tool_glob({"pattern": "*.py"}, cwd=tmp_path)
    assert res["files"] == ["sub/b.py"]


async def test_glob_caps_its_results_and_says_so(tmp_path, no_git_env):
    many = tmp_path / "many"
    many.mkdir()
    for i in range(MAX_SEARCH_RESULTS + 5):
        (many / f"f{i:04d}.txt").write_text("")
    res = await tool_glob({"pattern": "*.txt"}, cwd=tmp_path)
    assert len(res["files"]) == MAX_SEARCH_RESULTS and "truncated" in res["note"]
    assert format_tool_result(res).endswith(f"[{res['note']}]")


async def test_glob_and_the_grep_fallback_run_off_the_event_loop(tmp_path, no_git_env, monkeypatch):
    _tree(tmp_path)
    ran: list[str] = []
    real = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        ran.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # no rg (and no git): the Python fallback
    await tool_glob({"pattern": "*.py"}, cwd=tmp_path)
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
    assert len(ran) == 2
    hits = {Path(line.split(":")[0]).relative_to(tmp_path).as_posix() for line in res["matches"].splitlines()}
    assert hits == {"src/a.py", "src/pkg/b.py", "build/g.py", "README.md"}


async def test_the_grep_fallback_caps_its_matches(tmp_path, no_git_env, monkeypatch):
    (tmp_path / "big.txt").write_text("needle\n" * (MAX_SEARCH_RESULTS + 50))
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
    assert len(res["matches"].splitlines()) == MAX_SEARCH_RESULTS and "truncated" in res["warnings"]


async def test_project_prep_scans_risks_off_the_event_loop(tmp_path, monkeypatch):
    from k3code.autonomy.proposals import ProposalStore

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    ran: list[str] = []
    real = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        ran.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    await projectprep.prepare(tmp_path, store=ProposalStore(tmp_path / "k3home"))
    assert "scan_risks" in ran
