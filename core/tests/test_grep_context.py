"""grep shows lines of context on request and caps the matching lines it returns, saying how many more there were,
the same way with ripgrep and with the Python fallback."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from k3code.tools import GREP_DEFAULT_LIMIT, MAX_SEARCH_RESULTS, format_tool_result, tool_grep


@pytest.fixture(params=["rg", "fallback"])
def engine(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "rg":
        if not shutil.which("rg"):
            pytest.skip("rg not installed")
    else:
        monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # no rg: the fallback searches
    return request.param


def _rel(text: str, root: Path) -> str:
    return text.replace(f"{root}/", "").replace("./", "")


async def test_context_lines_are_marked_with_a_dash_and_groups_separated(tmp_path: Path, engine: str):
    (tmp_path / "a.txt").write_text("a\nneedle\nb\nc\nd\nneedle\ne\nneedle\nf\n")
    res = await tool_grep({"pattern": "needle", "context": 1}, cwd=tmp_path)
    assert _rel(res["matches"], tmp_path).splitlines() == [
        "a.txt:1-a",
        "a.txt:2:needle",
        "a.txt:3-b",
        "--",
        "a.txt:5-d",
        "a.txt:6:needle",
        "a.txt:7-e",  # the two groups touch: no separator between them
        "a.txt:8:needle",
        "a.txt:9-f",
    ]
    assert "warnings" not in res


async def test_without_context_the_output_is_as_before(tmp_path: Path, engine: str):
    (tmp_path / "a.txt").write_text("a\nneedle\nb\n")
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
    assert _rel(res["matches"], tmp_path) == "a.txt:2:needle"


async def test_the_cap_counts_matching_lines_not_context_and_says_how_many_more(tmp_path: Path, engine: str):
    (tmp_path / "a.txt").write_text("".join(f"x{i}\nneedle {i}\n" for i in range(50)))
    res = await tool_grep({"pattern": "needle", "context": 1, "limit": 3}, cwd=tmp_path)
    lines = _rel(res["matches"], tmp_path).splitlines()
    assert [ln for ln in lines if ":needle" in ln] == ["a.txt:2:needle 0", "a.txt:4:needle 1", "a.txt:6:needle 2"]
    assert lines[0] == "a.txt:1-x0" and not lines[-1].startswith("--")
    assert "truncated to the first 3 matching lines; 47 more not shown" in res["warnings"]
    assert "pass limit" in res["warnings"]
    assert "[truncated" in format_tool_result(res)


async def test_the_default_limit_is_200_matching_lines(tmp_path: Path, engine: str):
    (tmp_path / "a.txt").write_text("needle\n" * (GREP_DEFAULT_LIMIT + 7))
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
    assert len(res["matches"].splitlines()) == GREP_DEFAULT_LIMIT
    assert "7 more not shown" in res["warnings"]
    res = await tool_grep({"pattern": "needle", "limit": GREP_DEFAULT_LIMIT + 7}, cwd=tmp_path)
    assert len(res["matches"].splitlines()) == GREP_DEFAULT_LIMIT + 7 and "warnings" not in res


async def test_context_and_limit_are_bounded(tmp_path: Path, engine: str):
    (tmp_path / "a.txt").write_text("\n".join(str(i) for i in range(40)) + "\nneedle\n")
    res = await tool_grep({"pattern": "needle", "context": 99}, cwd=tmp_path)
    assert len(res["matches"].splitlines()) == 11  # at most 10 lines each way
    assert MAX_SEARCH_RESULTS == 1000  # `limit` is capped there (test_small_robustness)
