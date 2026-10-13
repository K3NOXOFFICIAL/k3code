"""grep, glob and edit results: the arguments Claude Code's models send are honoured instead of ignored, and a failed
search or edit says what is wrong instead of misleading (or spending three times the tokens it needs)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import k3code.tools as tools
from k3code.agent.loop import _ignored_arguments
from k3code.context_budget import text_tokens
from k3code.permissions.engine import _glob_base, decide
from k3code.tools import build_registry, format_tool_result, tool_edit, tool_glob, tool_grep
from k3code.tools.fuzzy_match import find_closest_lines, fuzzy_find_and_replace
from k3code.tools.validate import normalize_arguments

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _modules(root: Path, files: int = 15, lines: int = 100) -> None:
    (root / "src").mkdir()
    for i in range(files):
        body = "".join(
            f"def handler_{i}_{j}(request):\n    logger.info('TODO {j}')\n    return {j}\n" for j in range(lines)
        )
        (root / "src" / f"module_{i}.py").write_text(body)


# ── grep: output_mode, head_limit, context, type ──────────────────────────────────────────────────────────────────


@needs_rg
async def test_files_with_matches_lists_each_file_once_and_nothing_else(tmp_path):
    _modules(tmp_path)
    res = await tool_grep({"pattern": "logger.info", "output_mode": "files_with_matches"}, cwd=tmp_path)
    assert sorted(res["matches"].splitlines()) == sorted(f"src/module_{i}.py" for i in range(15))
    assert text_tokens(format_tool_result(res)) < 150, "the listing was 2500 tokens of matching lines"


@needs_rg
async def test_count_gives_the_matching_lines_per_file(tmp_path):
    _modules(tmp_path)
    res = await tool_grep({"pattern": "logger.info", "output_mode": "count"}, cwd=tmp_path)
    assert sorted(res["matches"].splitlines()) == sorted(f"src/module_{i}.py:100" for i in range(15))


@needs_rg
async def test_the_listing_modes_name_the_file_of_a_search_of_one_file(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 2\ny = 3\n")
    one = {"pattern": "x =", "path": "a.py"}
    assert (await tool_grep({**one, "output_mode": "count"}, cwd=tmp_path))["matches"] == "a.py:2"
    assert (await tool_grep({**one, "output_mode": "files_with_matches"}, cwd=tmp_path))["matches"] == "a.py"
    assert (await tool_grep({**one, "output_mode": "files_with_matches", "pattern": "zzz"}, cwd=tmp_path))[
        "matches"
    ] == ""


@needs_rg
async def test_the_listing_modes_never_name_a_credential_file(tmp_path):
    (tmp_path / "app.py").write_text("API_KEY = 1\n")
    (tmp_path / ".env.local").write_text("API_KEY=secret\n")
    for mode in ("files_with_matches", "count"):
        res = await tool_grep({"pattern": "API_KEY", "output_mode": mode, "include": "*"}, cwd=tmp_path)
        assert ".env" not in res["matches"] and "app.py" in res["matches"], (mode, res)


@needs_rg
async def test_head_limit_cuts_the_listing_before_the_size_clip_and_says_so(tmp_path):
    _modules(tmp_path)
    res = await tool_grep({"pattern": "logger.info", "head_limit": 5}, cwd=tmp_path)
    assert "head_limit" in res["warnings"] and "more" in res["warnings"]
    assert sum(line[:1].isdigit() for line in res["matches"].splitlines()) == 5, "five matching lines, one path"
    assert text_tokens(format_tool_result(res)) < 120
    files = await tool_grep(
        {"pattern": "logger.info", "output_mode": "files_with_matches", "head_limit": 3}, cwd=tmp_path
    )
    assert len(files["matches"].splitlines()) == 3 and "head_limit" in files["warnings"]
    # a limit above the number of results, and 0, cut nothing
    every = await tool_grep({"pattern": "logger.info", "output_mode": "count", "head_limit": 99}, cwd=tmp_path)
    assert len(every["matches"].splitlines()) == 15 and "warnings" not in every
    assert "warnings" not in await tool_grep({"pattern": "handler_1_1", "head_limit": 0}, cwd=tmp_path)


@needs_rg
async def test_context_lines_come_with_their_line_numbers_and_no_separator(tmp_path):
    (tmp_path / "a.txt").write_text(
        "".join(f"line {i}\n" for i in range(1, 21)).replace("line 3\n", "NEEDLE\n").replace("line 15\n", "NEEDLE\n")
    )
    (tmp_path / "b.txt").write_text("first\nNEEDLE\nlast\n")
    res = await tool_grep({"pattern": "NEEDLE", "-C": 1}, cwd=tmp_path)
    groups = sorted(res["matches"].split("\n\n"))  # rg searches the files in parallel: any order
    assert groups == [
        "a.txt\n2-line 2\n3:NEEDLE\n4-line 4\n14-line 14\n15:NEEDLE\n16-line 16",
        "b.txt\n1-first\n2:NEEDLE\n3-last",
    ]
    after = await tool_grep({"pattern": "NEEDLE", "path": "b.txt", "-A": 1}, cwd=tmp_path)
    assert after["matches"].splitlines() == ["2:NEEDLE", "3-last"]
    before = await tool_grep({"pattern": "NEEDLE", "path": "b.txt", "-B": 1}, cwd=tmp_path)
    assert before["matches"].splitlines() == ["1-first", "2:NEEDLE"]


@needs_rg
async def test_type_limits_the_search_to_one_kind_of_file_and_a_bad_type_is_rgs_error(tmp_path):
    (tmp_path / "a.py").write_text("NEEDLE\n")
    (tmp_path / "b.js").write_text("NEEDLE\n")
    res = await tool_grep({"pattern": "NEEDLE", "type": "py"}, cwd=tmp_path)
    assert res["matches"] == "a.py\n1:NEEDLE"
    bad = await tool_grep({"pattern": "NEEDLE", "type": "nosuchtype"}, cwd=tmp_path)
    assert "unrecognized file type" in bad["error"]


async def test_an_unknown_output_mode_is_refused_by_name(tmp_path):
    res = await tool_grep({"pattern": "x", "output_mode": "lines"}, cwd=tmp_path)
    assert "output_mode must be" in res["error"] and "'lines'" in res["error"]


def test_the_schemas_list_what_the_handlers_take_so_none_of_it_is_reported_as_ignored():
    reg = build_registry()
    grep, glob = reg.get("grep")[0], reg.get("glob")[0]
    args = {"pattern": "x", "output_mode": "count", "head_limit": 5, "-A": 1, "-B": 1, "-C": 2, "type": "py"}
    assert _ignored_arguments(grep, args) == ""
    assert _ignored_arguments(glob, {"pattern": "*.py", "head_limit": 3}) == ""
    assert grep.parameters["properties"]["output_mode"]["enum"] == ["content", "files_with_matches", "count"]
    # `-n` is satisfied (lines are always numbered), so it is not reported; `-n: false` cannot be honoured
    assert normalize_arguments("grep", {"pattern": "x", "-n": True}, grep.parameters) == {"pattern": "x"}
    assert normalize_arguments("grep", {"pattern": "x", "-n": False}, grep.parameters) == {"pattern": "x", "-n": False}


async def test_the_python_fallback_lists_counts_and_limits_too(tmp_path, monkeypatch):
    _modules(tmp_path, files=4, lines=3)

    async def no_rg(*_args, **_kwargs):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(tools, "_run_rg", no_rg)
    files = await tool_grep({"pattern": "logger.info", "output_mode": "files_with_matches"}, cwd=tmp_path)
    assert sorted(files["matches"].splitlines()) == [f"src/module_{i}.py" for i in range(4)]
    counts = await tool_grep({"pattern": "logger.info", "output_mode": "count"}, cwd=tmp_path)
    assert sorted(counts["matches"].splitlines()) == [f"src/module_{i}.py:3" for i in range(4)]
    cut = await tool_grep({"pattern": "logger.info", "head_limit": 2}, cwd=tmp_path)
    assert "head_limit" in cut["warnings"] and sum(line[:1].isdigit() for line in cut["matches"].splitlines()) == 2
    # context and type need rg: the result says they were not applied
    said = await tool_grep({"pattern": "logger.info", "-C": 2, "type": "py", "head_limit": 1}, cwd=tmp_path)
    assert "-C, type ignored: ripgrep is not installed" in said["warnings"]


@needs_git
async def test_glob_head_limit_keeps_the_first_paths_and_says_how_many_there_are(tmp_path):
    for i in range(7):
        (tmp_path / f"f{i}.py").write_text("x")
    res = await tool_glob({"pattern": "*.py", "head_limit": 3}, cwd=tmp_path)
    assert res["files"] == ["f0.py", "f1.py", "f2.py"] and res["note"] == "showing the first 3 of 7 (head_limit)"
    assert len((await tool_glob({"pattern": "*.py", "head_limit": 50}, cwd=tmp_path))["files"]) == 7
    assert "note" not in await tool_glob({"pattern": "*.py", "head_limit": 7}, cwd=tmp_path)


# ── grep: a regex that found nothing but whose text is there ──────────────────────────────────────────────────────


@needs_rg
@pytest.mark.parametrize(
    ("pattern", "line"),
    [("$5.00", 1), ("foo.bar[0]", 2), ("a+b", 3), ("list[i]", 4), ("f(x)", 4)],
)
async def test_a_regex_that_matches_nothing_but_whose_text_is_in_the_file_says_so(tmp_path, pattern, line):
    (tmp_path / "special.txt").write_text("price is $5.00 (approx)\nfoo.bar[0] = 1\na+b == c\nlist[i] = f(x)\n")
    shown = format_tool_result(await tool_grep({"pattern": pattern}, cwd=tmp_path))
    assert shown.startswith(f"[no matches. Check: its literal text occurs (1 place, first special.txt:{line})"), shown
    assert "escape" in shown and "backslash" in shown


@needs_rg
async def test_the_literal_hint_follows_the_case_flag_the_output_mode_and_the_filters(tmp_path):
    (tmp_path / "a.txt").write_text("Total: A+B\n")
    (tmp_path / "b.md").write_text("A+B\n")
    shown = format_tool_result(await tool_grep({"pattern": "(?i)a+b"}, cwd=tmp_path))
    assert "literal text occurs (2 places" in shown, "(?i) is a case-insensitive literal search"
    only_txt = format_tool_result(await tool_grep({"pattern": "A+B", "include": "*.txt"}, cwd=tmp_path))
    assert "(1 place, first a.txt:1)" in only_txt
    ctx = format_tool_result(await tool_grep({"pattern": "A+B", "include": "*.txt", "-C": 3}, cwd=tmp_path))
    assert "(1 place, first a.txt:1)" in ctx, "context lines are not places"
    by_file = format_tool_result(await tool_grep({"pattern": "A+B", "output_mode": "files_with_matches"}, cwd=tmp_path))
    assert "(2 places, first " in by_file and ":" not in by_file.split("first ")[1].split(")")[0]


@needs_rg
async def test_a_plain_miss_and_a_regex_miss_without_the_literal_text_stay_plain_misses(tmp_path):
    (tmp_path / "a.txt").write_text("hello world\n")
    assert format_tool_result(await tool_grep({"pattern": "zzz"}, cwd=tmp_path)) == "(no matches)"
    assert format_tool_result(await tool_grep({"pattern": "foo.*bar|baz"}, cwd=tmp_path)) == "(no matches)"
    assert format_tool_result(await tool_grep({"pattern": r"\bzzz\b"}, cwd=tmp_path)) == "(no matches)"


# ── grep and glob agree about .gitignore outside a work tree ──────────────────────────────────────────────────────


@needs_rg
async def test_grep_honours_gitignore_outside_a_git_work_tree_as_glob_does(tmp_path):
    (tmp_path / ".gitignore").write_text("gen/\n*.log\n")
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "out.js").write_text("NEEDLE gen\n")
    (tmp_path / "app.log").write_text("NEEDLE log\n")
    (tmp_path / "src.py").write_text("NEEDLE src\n")
    assert (await tool_grep({"pattern": "NEEDLE"}, cwd=tmp_path))["matches"] == "src.py\n1:NEEDLE src"
    assert (await tool_glob({"pattern": "**/*"}, cwd=tmp_path))["files"] == [".gitignore", "src.py"]
    # asked for on purpose, an ignored folder is still searched
    assert "gen/out.js" in (await tool_grep({"pattern": "NEEDLE", "path": "gen"}, cwd=tmp_path))["matches"]


# ── glob: what it leaves out ──────────────────────────────────────────────────────────────────────────────────────


@needs_git
async def test_an_empty_glob_says_when_gitignore_hides_the_matches_and_stays_short_for_a_typo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("build/\n*.log\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "app.js").write_text("x")
    (tmp_path / "server.log").write_text("x")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "x.log").write_text("x")
    (tmp_path / "main.py").write_text("x")
    log = format_tool_result(await tool_glob({"pattern": "*.log"}, cwd=tmp_path))
    assert log.startswith("(no files)\n[1 ignored by .gitignore match (server.log)") and "bash ls" in log, log
    assert "node_modules" not in log, "a skipped folder's files are not 'ignored matches'"
    inside = format_tool_result(await tool_glob({"pattern": "build/*"}, cwd=tmp_path))
    assert "(build/app.js)" in inside
    braces = format_tool_result(await tool_glob({"pattern": "*.{log,tmp}"}, cwd=tmp_path))
    assert "(server.log)" in braces
    typo = format_tool_result(await tool_glob({"pattern": "**/*.tsx"}, cwd=tmp_path))
    assert typo == "(no files)", "a plain miss is 3 tokens, not a paragraph about dependency folders"
    # what is listed is unchanged
    assert (await tool_glob({"pattern": "*.py"}, cwd=tmp_path))["files"] == ["main.py"]


@needs_git
async def test_the_ignored_matches_never_name_a_credential_file(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".env*\n")
    (tmp_path / ".env.local").write_text("KEY=secret\n")
    assert format_tool_result(await tool_glob({"pattern": ".env*"}, cwd=tmp_path)) == "(no files)"


@needs_rg
async def test_a_plain_search_runs_the_rg_command_it_always_did_plus_nothing_the_new_arguments_add(
    tmp_path, monkeypatch
):
    seen: list[list[str]] = []

    async def fake_rg(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return 1, [], b"", "", 0

    monkeypatch.setattr(tools, "_run_rg", fake_rg)
    await tool_grep({"pattern": "x"}, cwd=tmp_path)
    plain = seen[-1]
    for flag in ("--no-context-separator", "-l", "--count", "-t", "-A", "-B", "-C", "-F"):
        assert flag not in plain, flag
    await tool_grep({"pattern": "x", "-A": 1, "-C": 2}, cwd=tmp_path)
    assert seen[-1].count("--no-context-separator") == 1 and "-A" in seen[-1] and "-C" in seen[-1]
    await tool_grep({"pattern": "x", "-C": 0, "-A": True, "head_limit": -3}, cwd=tmp_path)
    assert seen[-1] == plain, "a zero, a bool and a negative number add nothing"


def test_the_glob_description_says_that_ignored_files_are_not_listed():
    assert ".gitignore" in build_registry().get("glob")[0].description


# ── glob: ~ ───────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_glob_expands_a_leading_tilde_and_the_permission_check_decides_the_same_directory(tmp_path, monkeypatch):
    home, proj = tmp_path / "home", tmp_path / "proj"
    (home / "notes").mkdir(parents=True)
    proj.mkdir()
    (home / "notes" / "a.txt").write_text("x")
    (home / "b.txt").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    assert (await tool_glob({"pattern": "~/*.txt"}, cwd=proj))["files"] == [  # *.txt recurses, as in any glob call
        str(home / "b.txt"),
        str(home / "notes" / "a.txt"),
    ]
    assert (await tool_glob({"pattern": "~/notes/*.txt"}, cwd=proj))["files"] == [str(home / "notes" / "a.txt")]
    assert _glob_base(str(proj), "~/notes/*.txt") == str(home / "notes/*.txt")
    # the call is outside the project, not a folder named "~" inside it: the project's own files need no question,
    # the home directory's do
    inside = decide(mode="default", tool="glob", args={"pattern": "src/*.py"}, cwd=proj)
    outside = decide(mode="default", tool="glob", args={"pattern": "~/*.txt"}, cwd=proj)
    assert inside.action == "allow" and outside.action != "allow", (inside, outside)
    # the same call spelled with the home directory spelled out: `~` is no way round the credential rules
    (home / ".ssh").mkdir()
    spelled = decide(mode="default", tool="glob", args={"pattern": f"{home}/.ssh/*"}, cwd=proj)
    tilde = decide(mode="default", tool="glob", args={"pattern": "~/.ssh/*"}, cwd=proj)
    assert (tilde.action, tilde.needs_human) == (spelled.action, spelled.needs_human) and tilde.action != "allow"
    assert _glob_base(str(proj), "~/.ssh/*") == str(home / ".ssh/*")


# ── edit: a block that exists nowhere is not "found" 12 times ─────────────────────────────────────────────────────

_TESTS = "".join(f"def test_case_{i}():\n    x = make({i})\n    assert x.value == {i}\n\n" for i in range(12))
_ASKED = "def test_case_3():\n    x = make(3)\n    assert x.value == 4"
_FIXED = "def test_case_3():\n    x = make(3)\n    assert x.value == 3"


def test_repeated_look_alike_blocks_are_no_match_not_twelve_matches():
    for replace_all in (False, True):
        new, count, strategy, error = fuzzy_find_and_replace(_TESTS, _ASKED, _FIXED, replace_all=replace_all)
        assert (new, count, strategy) == (_TESTS, 0, None)
        assert error.startswith("Could not find an exact match") and "12 blocks resemble" in error, error
        assert "Found" not in error


def test_real_duplicates_are_still_reported_as_matches():
    twice = "def f():\n    return 1\n\ndef g():\n    return 2\n\ndef f():\n    return 1\n"
    _, count, _, error = fuzzy_find_and_replace(twice, "def f():\n    return 1", "def f():\n    return 3")
    assert count == 0 and error.startswith("Found 2 matches for old_string")


async def test_a_failed_edit_names_the_closest_block_and_the_line_that_differs(tmp_path):
    f = tmp_path / "test_a.py"
    f.write_text(_TESTS)
    res = await tool_edit({"path": str(f), "old_string": _ASKED, "new_string": _FIXED}, cwd=tmp_path)
    err = res["error"]
    assert err.startswith("Could not find an exact match") and "Found" not in err
    assert "Did you mean this section?" in err
    assert "  13| def test_case_3():" in err and "test_case_9" not in err, "the one closest block, not twelve"
    assert err.endswith(
        "First difference at line 15:\n  file has: assert x.value == 3\n  you sent: assert x.value == 4"
    )
    assert f.read_text() == _TESTS


def _cache_module() -> str:
    def method(n: str) -> str:
        return (
            f"    def get_{n}(self, key, default=None):\n        value = self._store.get(key)\n"
            f"        if value is None:\n            return default\n        if not isinstance(value, {n}):\n"
            f'            raise TypeError("expected {n}, got %r" % type(value))\n        self._hits += 1\n'
            "        return value\n\n"
        )

    kinds = ["int", "str", "list", "dict", "float", "bytes", "set", "tuple", "bool"]
    return "class Cache:\n" + "".join(method(n) for n in kinds)


async def test_the_did_you_mean_hint_is_one_window_not_three_overlapping_ones(tmp_path):
    content = _cache_module()
    f = tmp_path / "cache.py"
    f.write_text(content)
    lines = content.splitlines()
    old = "\n".join(lines[19:63])  # five methods, 44 lines, from get_list on
    old = old.replace("def get_list(self, key, default=None):", "def get_list(self, key, default = None):", 1)
    old = old[: old.rindex("return value")] + "return value  # done"
    res = await tool_edit({"path": str(f), "old_string": old, "new_string": "    pass"}, cwd=tmp_path)
    err = res["error"]
    assert err.startswith("Could not find a match for old_string in the file\n\nDid you mean this section?")
    shown = [line for line in err.splitlines() if line[:4].strip().isdigit() and line[4:6] == "| "]
    assert len(shown) == len(old.splitlines()) + 4, "the block and two lines of context on each side, once"
    assert "---" not in err
    assert "First difference at line 20:\n  file has: def get_list(self, key, default=None):" in err
    assert text_tokens(err) < 700, "was 1359 tokens for a 360-token old_string"


def test_the_first_difference_is_found_past_an_insertion_and_in_the_indentation():
    content = "def f():\n    a = 1\n    b = 2\n    c = 3\n    return a\n"
    # one line too few in the middle: the first difference is the file's extra line, not a shifted pair
    hint = find_closest_lines("def f():\n    a = 1\n    c = 3\n    return a", content)
    assert "First difference at line 3: the file has b = 2, old_string has no such line" in hint
    # the same text, the second line indented with a tab
    hint = find_closest_lines("def f():\n\ta = 1\n    b = 2", content)
    assert "First difference at line 2, the indentation (→ = tab, · = space):" in hint
    assert "file has: ····a = 1" in hint and "you sent: →a = 1" in hint
    # a line only the old_string has
    hint = find_closest_lines("def f():\n    a = 1\n    zzz = 9\n    b = 2\n    c = 3", content)
    assert "First difference: old_string line 3 is not in the file: zzz = 9" in hint
    # nothing alike: no hint at all
    assert find_closest_lines("qqqq wwww", content) == ""


def test_the_window_starts_where_a_leading_blank_line_of_old_string_puts_it():
    content = "x = 0\n\ndef f():\n    a = 1\n    b = 2\ny = 5\n"
    hint = find_closest_lines("\ndef f():\n    a = 1\n    b = 3", content, context_lines=0)
    assert hint.startswith("   2| \n   3| def f():") or hint.startswith("   3| def f():"), hint
    assert "First difference at line 5:\n  file has: b = 2\n  you sent: b = 3" in hint
