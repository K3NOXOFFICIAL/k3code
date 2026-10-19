"""The grep fallback (no rg on PATH) is bounded: a pattern the model wrote that backtracks for minutes on one line
must neither freeze the event loop (Python's ``re`` holds the GIL for a whole match) nor run past the time budget.
The match runs in a worker process the fallback kills at the deadline."""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path

import pytest

import k3code.tools as tools
from k3code.tools import tool_grep

CATASTROPHIC = "(a+)+b"  # exponential backtracking on a run of "a" with no "b"


@pytest.fixture
def no_rg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # no rg, no git: the Python fallback walks the tree


async def _grep_with_ticker(args: dict, cwd: Path) -> tuple[dict, float, float]:
    """(result, wall seconds, longest gap between two ticks of a 10 ms ticker on the event loop)."""
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    start = time.monotonic()
    try:
        res = await tool_grep(args, cwd=cwd)
    finally:
        done.set()
        await task
    return res, time.monotonic() - start, max(gaps, default=0.0)


@pytest.mark.parametrize("named", [True, False], ids=["named-file", "walk"])
async def test_a_catastrophic_pattern_is_cut_at_the_budget_without_freezing_the_loop(
    tmp_path, no_rg, monkeypatch, named
) -> None:
    monkeypatch.setattr(tools, "SEARCH_BUDGET_S", 2.0)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ok.txt").write_text("nothing here\n")
    (proj / "evil.txt").write_text("a" * 40 + "c\n")  # backtracking would take hours
    args = {"pattern": CATASTROPHIC, **({"path": str(proj / "evil.txt")} if named else {})}
    res, wall, stall = await _grep_with_ticker(args, proj)
    assert wall < 5.0
    assert stall < 0.25
    assert res["matches"] == ""
    assert "results are partial" in res["warnings"]
    assert "ripgrep (rg) is not installed" in res["warnings"]


async def test_a_slow_pattern_that_finishes_in_the_budget_does_not_freeze_the_loop(tmp_path, no_rg) -> None:
    """26 a: about 3 s of backtracking, inside the budget; the loop kept running the whole time."""
    (tmp_path / "slow.txt").write_text("a" * 26 + "c\naab\n")
    res, _wall, stall = await _grep_with_ticker({"pattern": CATASTROPHIC}, tmp_path)
    assert stall < 0.25
    assert res["matches"] == f"{tmp_path / 'slow.txt'}:2:aab"
    assert "warnings" not in res


async def test_a_regex_skips_a_very_long_line_and_says_so_while_a_literal_searches_it(tmp_path, no_rg) -> None:
    (tmp_path / "bundle.js").write_text("x" * 30_000 + " needle\nneedle short\n")
    res = await tool_grep({"pattern": "need+le"}, cwd=tmp_path)
    assert res["matches"] == f"{tmp_path / 'bundle.js'}:2:needle short"
    assert "1 line(s) longer than 20000 characters" in res["warnings"]
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)  # literal: no regex, no cap
    assert [line.split(":")[1] for line in res["matches"].splitlines()] == ["1", "2"]
    assert "warnings" not in res


@pytest.mark.parametrize("rg", [None, "/usr/bin/rg"], ids=["no-rg", "rg"])
@pytest.mark.parametrize("walk_done", [True, False], ids=["walk-done", "walk-cut"])
def test_a_cut_result_says_how_much_was_searched_and_why_it_was_slow(monkeypatch, rg, walk_done) -> None:
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: rg if name == "rg" else None)
    feed = tools._Feed(sent=5000, walk_cut=not walk_done, walk_done=walk_done)
    res = tools._grep_result(["a.py:1:x"], feed, 1234, 0, cut=True)
    total = "~5000" if walk_done else "an unknown number of"
    note = (
        f"searched 1234 of {total} files before the 10 s budget; results are partial; "
        "narrow the path or add an include glob"
    )
    slow = "; ripgrep (rg) is not installed, which makes this search much slower"
    assert res == {"matches": "a.py:1:x", "warnings": note + (slow if rg is None else "")}


def test_a_complete_result_says_nothing_extra(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None)
    res = tools._grep_result(["a.py:1:x"], tools._Feed(sent=3, walk_done=True), 3, 0, cut=False)
    assert res == {"matches": "a.py:1:x"}


async def test_a_search_cut_by_a_tiny_budget_counts_the_files_it_searched(tmp_path, no_rg, monkeypatch) -> None:
    """The worker gets through some files, then the backtracking one holds it past the deadline: N is its last
    progress count (written every 50 ms, so at most the five files before it), M the files the walk handed over (the
    walk is done long before)."""
    monkeypatch.setattr(tools, "SEARCH_BUDGET_S", 1.0)
    for i in range(5):
        (tmp_path / f"a{i}.txt").write_text("nothing\n")
    (tmp_path / "z.txt").write_text("a" * 40 + "c\n")  # sorted last: searched after the five others
    res = await tool_grep({"pattern": CATASTROPHIC}, cwd=tmp_path)
    assert res["matches"] == ""
    searched = re.match(
        r"searched (\d) of ~6 files before the 1 s budget; results are partial; narrow", res["warnings"]
    )
    assert searched and int(searched[1]) <= 5, res
    assert res["warnings"].endswith("ripgrep (rg) is not installed, which makes this search much slower")
