"""Tool calls that failed without saying why, succeeded at the wrong thing, or cost too many tokens."""

from __future__ import annotations

import time

from k3code.providers.types import ToolSpec
from k3code.reliability.loopguard import LoopGuard, Verdict
from k3code.tools import (
    ToolRegistry,
    format_tool_result,
    tool_bash,
    tool_edit,
    tool_glob,
    tool_grep,
    tool_read,
)
from k3code.tools.validate import check_arguments, drop_null_options

# ── bash ──────────────────────────────────────────────────────────────


async def test_a_command_that_leaves_a_child_running_returns_when_it_exits(tmp_path):
    """`sleep 100 & echo started` waited for the child's pipes and reported a timeout, with the work done."""
    started = time.monotonic()
    res = await tool_bash({"command": "sleep 100 & echo started", "timeout": 20}, cwd=tmp_path)
    assert time.monotonic() - started < 6
    assert res["exit_code"] == 0 and res["stdout"] == "started\n" and "error" not in res
    text = format_tool_result(res)
    assert text.startswith("started") and "background was stopped" in text and "exit code" not in text


async def test_a_plain_command_has_no_note_and_a_slow_one_still_times_out(tmp_path):
    ok = await tool_bash({"command": "echo hi; echo oops >&2"}, cwd=tmp_path)
    assert ok == {"stdout": "hi\n", "stderr": "oops\n", "exit_code": 0}
    slow = await tool_bash({"command": "sleep 30", "timeout": 1}, cwd=tmp_path)
    assert "timed out after 1s" in slow["error"] and slow["exit_code"] == -1


async def test_ansi_codes_are_not_sent_to_the_model(tmp_path):
    res = await tool_bash({"command": "printf '\\033[31mred\\033[0m \\033[2K\\r50%%\\n'"}, cwd=tmp_path)
    assert "\x1b" in res["stdout"]  # the transcript keeps what the tool printed
    assert format_tool_result(res) == "red \r50%"


# ── grep and glob ─────────────────────────────────────────────────────


async def test_grep_and_glob_on_a_missing_path_say_so(tmp_path):
    grep = await tool_grep({"pattern": "x", "path": "nope"}, cwd=tmp_path)
    assert grep["error"] == "Path not found: nope"
    assert "Directory not found" in (await tool_glob({"pattern": "*.py", "path": "nope"}, cwd=tmp_path))["error"]


async def test_an_invalid_regex_is_reported_by_grep(tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    res = await tool_grep({"pattern": "foo("}, cwd=tmp_path)
    assert res["error"].startswith("grep failed:") and "unclosed group" in res["error"]


async def test_grep_prints_paths_relative_to_the_session_and_cuts_long_lines(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle\n" + "needle " + "x" * 2000 + "\n")
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
    lines = res["matches"].splitlines()
    assert lines[0] == "src/a.py:1:needle" and all(len(line) < 400 for line in lines)
    inside = await tool_grep({"pattern": "needle", "path": "src"}, cwd=tmp_path)
    assert inside["matches"].splitlines()[0] == "src/a.py:1:needle"


async def test_grep_skips_node_modules_without_a_gitignore(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("needle\n")
    (tmp_path / "app.js").write_text("needle\n")
    res = await tool_grep({"pattern": "needle"}, cwd=tmp_path)
    assert res["matches"] == "app.js:1:needle"


async def test_glob_takes_braces_and_absolute_patterns(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.md").write_text("")
    (tmp_path / "c.txt").write_text("")
    assert (await tool_glob({"pattern": "**/*.{py,md}"}, cwd=tmp_path))["files"] == ["a.py", "b.md"]
    assert (await tool_glob({"pattern": f"{tmp_path}/*.py"}, cwd=tmp_path))["files"] == ["a.py"]
    assert (await tool_glob({"pattern": "*.{py}"}, cwd=tmp_path))["files"] == []  # `{x}` is no alternation


# ── read and edit ─────────────────────────────────────────────────────


async def test_read_explains_a_directory_a_typo_and_a_binary_file(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "main.py").write_text("x = 1\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01" * 500)
    assert "is a directory" in (await tool_read({"path": "sub"}, cwd=tmp_path))["error"]
    assert "Similar files in that directory: main.py" in (await tool_read({"path": "mian.py"}, cwd=tmp_path))["error"]
    assert "does not exist" in (await tool_read({"path": "nodir/x.py"}, cwd=tmp_path))["error"]
    assert "binary file" in (await tool_read({"path": "blob.bin"}, cwd=tmp_path))["error"]
    assert (
        "Similar files"
        in (await tool_edit({"path": "mian.py", "old_string": "a", "new_string": "b"}, cwd=tmp_path))["error"]
    )


async def test_a_short_edit_never_lands_on_a_similar_looking_line(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("def f():\n    x = 2\n    retries = 3\n    return x\n")
    res = await tool_edit({"path": "f.py", "old_string": "    x = 1", "new_string": "    x = 10"}, cwd=tmp_path)
    assert "error" in res and f.read_text().count("x = 2") == 1
    exact = await tool_edit({"path": "f.py", "old_string": "    x = 2", "new_string": "    x = 20"}, cwd=tmp_path)
    assert exact["strategy"] == "exact" and "x = 20" in f.read_text()


async def test_a_block_of_several_lines_still_matches_loosely(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("def f():\n    a = compute(1)\n    b = compute(2)\n    return a + b\n")
    old = "def f():\n    a = compute(1)\n    b = compute( 2)\n    return a + b\n"
    res = await tool_edit({"path": "f.py", "old_string": old, "new_string": "def f():\n    return 3\n"}, cwd=tmp_path)
    assert res.get("ok") and f.read_text() == "def f():\n    return 3\n"


async def test_a_resent_edit_answers_that_nothing_is_left_to_do(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("value = compute_the_answer()\n")
    args = {"path": "f.py", "old_string": "value = 1\n", "new_string": "value = compute_the_answer()\n"}
    res = await tool_edit(args, cwd=tmp_path)
    assert "error" not in res and "No change needed" in format_tool_result(res)


# ── the arguments ─────────────────────────────────────────────────────

READ = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "tag": {"type": ["string", "null"]}},
    "required": ["path"],
}


def test_malformed_json_arguments_are_called_that():
    (problem,) = check_arguments(READ, {"_unparsed": '{"path": "a.py", '})
    assert problem.startswith("the arguments were not a valid JSON object") and '"path": "a.py"' in problem
    assert "missing required" not in problem
    assert "not a valid JSON object" in check_arguments(READ, {"_raw": ["a.py"]})[0]


def test_optional_arguments_sent_as_null_are_dropped_but_not_required_ones():
    assert drop_null_options({"path": "a.py", "offset": None}, READ) == {"path": "a.py"}
    assert drop_null_options({"path": None}, READ) == {"path": None}  # still reported as the wrong type
    assert drop_null_options({"path": "a", "tag": None}, READ) == {"path": "a", "tag": None}  # null is allowed there


# ── the registry ──────────────────────────────────────────────────────


def test_activated_tools_follow_the_built_in_ones_in_activation_order():
    reg = ToolRegistry()

    async def handler(arguments, *, cwd=None):
        return {}

    for name, deferred in (("read", False), ("mcp__a", True), ("mcp__b", True), ("bash", False), ("mcp__c", True)):
        reg.register(ToolSpec(name=name, description="", parameters={"type": "object"}), handler, deferred=deferred)
    assert [s.name for s in reg.specs()] == ["read", "bash"]
    reg.activate(["mcp__c", "mcp__a", "nope", "mcp__c"])
    assert [s.name for s in reg.specs()] == ["read", "bash", "mcp__c", "mcp__a"]
    reg.activate(["mcp__b"])
    assert reg.active_names() == ["mcp__c", "mcp__a", "mcp__b"]  # earlier ones keep their place


# ── the loop guard ────────────────────────────────────────────────────


def poll(guard: LoopGuard, digest: str) -> Verdict:
    outcome = guard.observe_tool_call("bash_output", {"job_id": "job1"})
    guard.observe_tool_result("bash_output", {"job_id": "job1"}, None, "bash_output job1", digest)
    return outcome.verdict


def test_polling_a_job_whose_output_changes_is_not_a_loop():
    guard = LoopGuard()
    assert [poll(guard, f"progress {i}") for i in range(10)] == [Verdict.OK] * 10


def test_the_same_request_with_the_same_result_still_trips_the_guard():
    guard = LoopGuard()
    verdicts = [poll(guard, "no change") for _ in range(5)]
    assert verdicts[:2] == [Verdict.OK, Verdict.OK]
    assert verdicts[2] is Verdict.NOTE and verdicts[3] is Verdict.STOP


# ── web_fetch ─────────────────────────────────────────────────────────


def test_a_long_page_keeps_its_start_and_says_how_much_of_it_that_is():
    from k3code.research.tools import MAX_FETCH_CHARS, _cap_page

    page = "p" * 30_000
    capped = _cap_page(page, "\n[page cut: the size cap was reached]")
    assert len(capped) <= MAX_FETCH_CHARS < 10_000  # no second, silent clip on the way to the model
    assert capped.startswith("p" * 9_000)
    assert "of 30000 characters are shown]" in capped and capped.endswith("the size cap was reached]")
    assert _cap_page("short page") == "short page"
    assert len(_cap_page("x" * (MAX_FETCH_CHARS - 5), "\n[note]")) <= MAX_FETCH_CHARS
