"""Small robustness fixes: skill-use outcomes, atomic .usage.json, mem0 posts once, glob/grep tree rules and caps,
blocking scans off the event loop."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
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


def _mem0_cfg() -> Settings:
    return Settings(mem0=Mem0Config(url="http://mem0.test", api_key_env="NOPE", agent_id="a"))


def _twelve() -> list[distiller.Preference]:
    return [distiller.Preference(f"fact number {i}", 0.6, 1) for i in range(12)]


def test_mem0_outage_stops_the_pass_at_the_first_failed_post(tmp_path):
    attempts: list[str] = []

    def down(url, body, headers):
        attempts.append(body["messages"][0]["content"])
        raise httpx.ConnectTimeout("blackhole")

    assert distiller.store_mem0(_mem0_cfg(), _twelve(), down, tmp_path / "posted.json") == 0
    assert len(attempts) == 1  # it was 12: about 120 s of a worker thread against a host that does not answer


def test_unposted_mem0_items_wait_in_an_outbox_and_go_out_on_the_next_pass(tmp_path):
    posted, outbox = tmp_path / "posted.json", tmp_path / "learning" / "mem0_outbox.json"
    cfg = _mem0_cfg()

    def down(url, body, headers):
        raise httpx.ConnectError("refused")

    assert distiller.store_mem0(cfg, _twelve(), down, posted, outbox) == 0
    assert [e["text"] for e in json.loads(outbox.read_text())] == [p.text for p in _twelve()]
    sent: list[str] = []
    # the next pass has nothing new (a review fact is not derived again), yet the queued twelve go out, oldest first
    assert distiller.store_mem0(cfg, [], lambda u, b, h: sent.append(b["messages"][0]["content"]), posted, outbox) == 12
    assert sent == [f"User preference (learned by k3code): fact number {i}" for i in range(12)]
    assert not outbox.exists()
    # and are not posted a third time
    assert distiller.store_mem0(cfg, _twelve(), lambda *a: sent.append("again"), posted, outbox) == 0
    assert len(sent) == 12


def test_mem0_outbox_keeps_what_the_outage_cut_off_and_does_not_repeat_itself(tmp_path):
    posted, outbox = tmp_path / "posted.json", tmp_path / "mem0_outbox.json"
    cfg = _mem0_cfg()
    calls: list[str] = []

    def dies_on_the_third(url, body, headers):
        calls.append(body["messages"][0]["content"])
        if len(calls) == 3:
            raise httpx.ReadTimeout("slow")

    assert distiller.store_mem0(cfg, _twelve(), dies_on_the_third, posted, outbox) == 2
    queued = [e["text"] for e in json.loads(outbox.read_text())]
    assert queued == [f"fact number {i}" for i in range(2, 12)]  # the failed one and the nine never tried
    # the same preferences derived again do not queue twice
    assert distiller.store_mem0(cfg, _twelve(), lambda *a: 1 / 0, posted, outbox) == 0
    assert [e["text"] for e in json.loads(outbox.read_text())] == queued


def test_mem0_refusal_of_one_item_does_not_stop_the_pass_or_clog_the_outbox(tmp_path):
    posted, outbox = tmp_path / "posted.json", tmp_path / "mem0_outbox.json"
    sent: list[str] = []

    def refuses_the_second(url, body, headers):
        text = body["messages"][0]["content"]
        if text.endswith("fact number 1"):
            req = httpx.Request("POST", url)
            raise httpx.HTTPStatusError("bad", request=req, response=httpx.Response(422, request=req))
        sent.append(text)

    assert distiller.store_mem0(_mem0_cfg(), _twelve(), refuses_the_second, posted, outbox) == 11
    assert len(sent) == 11
    assert not outbox.exists()  # a body mem0 refuses will not be accepted later; it must not block the queue


def test_mem0_outbox_is_bounded_and_private(tmp_path):
    outbox = tmp_path / "learning" / "mem0_outbox.json"
    many = [distiller.Preference(f"fact number {i}", 0.6, 1) for i in range(distiller.MEM0_OUTBOX_MAX + 30)]
    old_umask = os.umask(0)
    try:
        distiller.store_mem0(_mem0_cfg(), many, lambda *a: 1 / 0, tmp_path / "posted.json", outbox)
    finally:
        os.umask(old_umask)
    kept = [e["text"] for e in json.loads(outbox.read_text())]
    assert len(kept) == distiller.MEM0_OUTBOX_MAX and kept[-1] == f"fact number {len(many) - 1}"  # the newest stay
    assert outbox.stat().st_mode & 0o777 == 0o600 and outbox.parent.stat().st_mode & 0o777 == 0o700


def test_mem0_post_connects_within_two_seconds(monkeypatch):
    seen: dict[str, object] = {}

    def fake_post(url, **kw):
        seen.update(kw)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    assert distiller.store_mem0(_mem0_cfg(), _twelve()[:1]) == 1
    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout) and timeout.connect == 2 and timeout.read == 10


def _tree(root: Path) -> None:
    for rel in (
        "src/a.py",
        "src/pkg/b.py",
        "node_modules/x/c.py",
        ".venv/lib/d.py",
        "dist/e.py",
        ".git/hooks/f.py",
        "build/g.py",
        ".cache/uv/h.py",
        "pkg/__pycache__/i.py",
        ".claude/worktrees/w1/j.py",
        ".claude/jobs/k/tmp/k.py",
        ".claude/agents/l.py",
        "README.md",
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("needle\n")


async def test_glob_skips_vendor_dirs_without_git(tmp_path, no_git_env):
    _tree(tmp_path)
    res = await tool_glob({"pattern": "*.py"}, cwd=tmp_path)
    # outside a work tree caches, build output and other sessions' worktrees are skipped like ripgrep would;
    # other dot directories stay searchable
    assert res["files"] == [".claude/agents/l.py", "src/a.py", "src/pkg/b.py"]
    assert (await tool_glob({"pattern": "src/*.py"}, cwd=tmp_path))["files"] == ["src/a.py"]
    assert (await tool_glob({"pattern": "**/pkg"}, cwd=tmp_path))["files"] == ["src/pkg"]


async def test_glob_honours_gitignore_in_a_work_tree(tmp_path, no_git_env):
    assert shutil.which("git")
    _tree(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\nnode_modules/\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    assert (tmp_path / ".git" / "HEAD").is_file()  # the repository is the test's own
    res = await tool_glob({"pattern": "*.py"}, cwd=tmp_path)
    # build/ ignored by .gitignore, caches by SKIP_DIRS, other sessions' worktrees by SKIP_SUBPATHS
    assert res["files"] == [".claude/agents/l.py", "src/a.py", "src/pkg/b.py"]


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
    assert hits == {"src/a.py", "src/pkg/b.py", ".claude/agents/l.py", "README.md"}


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


async def test_glob_without_git_uses_the_same_skips_with_and_without_rg(tmp_path, no_git_env, monkeypatch):
    _tree(tmp_path)
    (tmp_path / ".gitignore").write_text("src/pkg/\n")  # rg honours it even outside a repository
    has_rg = bool(shutil.which("rg"))
    with_rg = (await tool_glob({"pattern": "*.py"}, cwd=tmp_path))["files"]
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    walked = (await tool_glob({"pattern": "*.py"}, cwd=tmp_path))["files"]
    assert walked == [".claude/agents/l.py", "src/a.py", "src/pkg/b.py"]
    if has_rg:
        assert with_rg == [".claude/agents/l.py", "src/a.py"]


async def test_glob_and_grep_stop_at_the_time_budget_with_a_hint(tmp_path, no_git_env, monkeypatch):
    import k3code.tools as tools

    _tree(tmp_path)
    monkeypatch.setattr(tools, "SEARCH_BUDGET_S", -1.0)  # already past the deadline
    res = await tool_glob({"pattern": "*.py"}, cwd=tmp_path)
    assert res["files"] == [] and "stopped after" in res["note"] and "narrow" in res["note"]
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # the Python grep fallback
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
    assert "stopped after" in res["warnings"]


async def test_grep_stops_reading_at_the_result_cap(tmp_path, no_git_env):
    if not shutil.which("rg"):
        pytest.skip("rg not installed")
    (tmp_path / "big.txt").write_text("needle\n" * (MAX_SEARCH_RESULTS * 3))
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
    assert len(res["matches"].splitlines()) == MAX_SEARCH_RESULTS
    assert "truncated" in res["warnings"]


def _git_init(root: Path) -> None:
    assert shutil.which("git")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    assert (root / ".git" / "HEAD").is_file()  # the repository is the test's own


async def test_glob_in_a_work_tree_stats_only_its_hits(tmp_path, no_git_env, monkeypatch):
    # An lstat per listed file took about 157 s for a 45k-file checkout on WSL /mnt/c (#77)
    for i in range(200):
        (tmp_path / f"f{i:03d}.txt").write_text("")
    (tmp_path / "a.py").write_text("")
    _git_init(tmp_path)
    checked: list[str] = []
    real = os.path.lexists

    def spy(p):
        checked.append(Path(p).name)
        return real(p)

    monkeypatch.setattr(os.path, "lexists", spy)
    res = await tool_glob({"pattern": "*.py"}, cwd=tmp_path)
    assert res["files"] == ["a.py"]
    assert checked == ["a.py"]


def test_the_work_tree_listing_stops_at_the_deadline(tmp_path, no_git_env):
    import time

    from k3code.tools import _git_files, _iter_tree

    _tree(tmp_path)
    _git_init(tmp_path)
    assert _git_files(tmp_path)  # the git listing, not the rg or walk fallback, is what has to stop
    assert list(_iter_tree(tmp_path, time.monotonic() - 1.0)) == [(None, False)]


async def test_the_grep_fallback_skips_a_200_mb_binary_without_reading_it(tmp_path, no_git_env, monkeypatch):
    import tracemalloc

    with (tmp_path / "big.bin").open("wb") as f:
        f.write(b"needle\n")
        f.truncate(200 * 1024 * 1024)  # sparse: NUL bytes after the first line, nothing on disk
    (tmp_path / "a.py").write_text("needle\n")
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    tracemalloc.start()
    try:
        res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert res["matches"] == f"{tmp_path / 'a.py'}:1:needle"
    assert peak < 16 * 1024 * 1024


async def test_the_grep_fallback_streams_a_large_text_file(tmp_path, no_git_env, monkeypatch):
    import tracemalloc

    big = tmp_path / "big.log"
    with big.open("w") as f:
        for _ in range(64 * 1024):
            f.write("x" * 1023 + "\n")
        f.write("needle\n")
        f.write("y" * (8 * 1024 * 1024) + " needle\n")  # one 8 MB line: read in bounded pieces, not whole
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    tracemalloc.start()
    try:
        res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    hits = [line.split(":")[1] for line in res["matches"].splitlines()]
    assert hits == [str(64 * 1024 + 1), str(64 * 1024 + 2)]
    assert peak < 16 * 1024 * 1024


@pytest.mark.parametrize("line,count", [("x\n", 500_000), ("x" * (2 << 20) + "\n", 8)], ids=["short", "long"])
def test_the_grep_fallback_checks_the_deadline_inside_a_file(tmp_path, monkeypatch, line, count):
    import types

    import k3code.tools as tools

    ticks = iter(range(1_000_000))
    monkeypatch.setattr(tools, "time", types.SimpleNamespace(monotonic=lambda: float(next(ticks))))
    big = tmp_path / "big.txt"
    big.write_text(line * count)  # no match, so only the clock can stop the search before the end of the file
    res = tools._grep_fallback("needle", big, None, None)
    assert "stopped after" in res.get("warnings", "")


async def test_the_grep_fallback_cuts_long_matching_lines_like_rg(tmp_path, no_git_env, monkeypatch):
    import tracemalloc

    (tmp_path / "bundle.min.js").write_text(("needle" + "y" * 1018) * 32 * 1024)  # 32 MB on one line
    (tmp_path / "a.js").write_text("x" * 500 + " needle\n")
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    tracemalloc.start()
    try:
        res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert f"{tmp_path / 'a.js'}:1:{'x' * 400} [... omitted end of long line]" in res["matches"].splitlines()
    assert peak < 16 * 1024 * 1024


async def test_the_grep_fallback_says_a_named_binary_file_matches(tmp_path, no_git_env, monkeypatch):
    lib = tmp_path / "lib.so"
    lib.write_bytes(b"\x7fELF\x00\x00 SYMBOL_NAME\n" + b"\x00" * 100)
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    res = await tool_grep({"pattern": "SYMBOL_NAME", "path": str(lib)}, cwd=tmp_path)
    assert res["matches"] == f"{lib}: binary file matches"
    assert (await tool_grep({"pattern": "absent", "path": str(lib)}, cwd=tmp_path))["matches"] == ""
