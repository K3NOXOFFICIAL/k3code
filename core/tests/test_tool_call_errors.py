"""Tool calls that fail for a reason the model could have been told: what counts toward the stop, what a failure says,
and the small sharp edges of bash, grep, glob, edit and the argument parsers."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from k3code.agent.loop import AgentLoop, _call_failed
from k3code.providers.anthropic import _parse_args as anthropic_parse
from k3code.providers.openai_compat import _parse_args as openai_parse
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.reliability import sandbox
from k3code.router import Router, build_chain
from k3code.tools import IGNORED_KEY, format_tool_result, tool_bash, tool_glob, tool_grep
from k3code.tools.validate import check_arguments


class Replies:
    """A model that answers each request with the next list of tool calls, then with text."""

    name = "fake"
    base_url = "https://fake.test"

    def __init__(self, replies: list[list[ToolCall]]) -> None:
        self.replies, self.n = replies, 0
        self.seen: list[list[Message]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.seen.append(list(messages))
        calls = self.replies[self.n] if self.n < len(self.replies) else []
        self.n += 1
        msg = Message(role="assistant", content=None if calls else "done", tool_calls=calls)
        yield StreamEvent(type="done", message=msg, usage=Usage())

    async def aclose(self) -> None:
        pass


def loop_for(tmp: Path, provider: Replies, **kw) -> AgentLoop:
    kw.setdefault("max_tool_errors", 8)
    return AgentLoop(
        Router(build_chain([provider], [["m"]]), max_retries=0),
        system_prompt="t",
        permission_mode="yolo",
        cwd=tmp,
        max_turns=40,
        **kw,
    )


async def drain(loop: AgentLoop, prompt: str = "go") -> list[StreamEvent]:
    return [e async for e in loop.run(prompt)]


def read_call(i: int, path: str = "") -> ToolCall:
    return ToolCall(id=f"r{i}", name="read", arguments={"path": path or f"missing{i}.txt"})


def bash_call(i: int, command: str) -> ToolCall:
    return ToolCall(id=f"b{i}", name="bash", arguments={"command": command})


# ── what counts toward the stop ───────────────────────────────────────────────────────────────────────────────────


async def test_one_reply_of_many_failing_parallel_calls_is_one_step_not_a_stop(tmp_path):
    provider = Replies([[read_call(i) for i in range(10)]])
    loop = loop_for(tmp_path, provider)
    await drain(loop)
    assert loop.escalation_reason is None
    assert provider.n == 2, "the model saw all ten errors and answered"


async def test_eight_failing_replies_in_a_row_still_stop_the_turn(tmp_path):
    provider = Replies([[read_call(i)] for i in range(20)])
    loop = loop_for(tmp_path, provider)
    events = await drain(loop)
    assert loop.escalation_reason == "tool_errors" and provider.n == 8
    assert "8 tool calls in a row failed" in str(events[-1].message.content)


async def test_the_stop_message_counts_every_failed_call_not_the_fifty_it_lists(tmp_path):
    provider = Replies([[read_call(i * 8 + j) for j in range(8)] for i in range(10)])
    loop = loop_for(tmp_path, provider)
    events = await drain(loop)
    assert loop.escalation_reason == "tool_errors" and loop.failed_calls == 64
    said = str(events[-1].message.content)
    assert said.startswith("I stopped because 64 tool calls in a row failed (the last 50 are listed):")


async def test_a_reply_with_one_success_resets_the_count(tmp_path):
    (tmp_path / "ok.txt").write_text("fine\n")
    ok = ToolCall(id="ok", name="read", arguments={"path": "ok.txt"})
    replies = [[read_call(i), read_call(i + 100)] for i in range(5)] + [[read_call(50), ok]]
    replies += [[read_call(60 + i), read_call(70 + i)] for i in range(5)]
    provider = Replies(replies)
    loop = loop_for(tmp_path, provider)
    await drain(loop)
    assert loop.escalation_reason is None, "5 + a mixed reply + 5 never reaches 8 in a row"


async def test_a_bash_command_that_keeps_exiting_with_an_error_stops_the_turn(tmp_path):
    provider = Replies([[bash_call(i, f"echo boom{i} >&2; exit 3")] for i in range(20)])
    loop = loop_for(tmp_path, provider)
    await drain(loop)
    assert loop.escalation_reason == "tool_errors" and provider.n == 8


async def test_a_grep_that_finds_nothing_is_not_a_failure(tmp_path):
    provider = Replies([[bash_call(i, f"grep -rn nosuchsymbol{i} .")] for i in range(20)] + [[]])
    loop = loop_for(
        tmp_path,
        provider,
    )
    await drain(loop)
    assert loop.escalation_reason is None and provider.n == 21


@pytest.mark.parametrize(
    ("result", "failed"),
    [
        ({"error": "boom"}, True),
        ({"error": "boom", "content": "partial"}, False),
        ({"stdout": "", "stderr": "", "exit_code": 0}, False),
        ({"stdout": "", "stderr": "", "exit_code": 1}, False),
        ({"stdout": "", "stderr": "fatal: not a git repository", "exit_code": 1}, True),
        ({"stdout": "", "stderr": "", "exit_code": 2}, True),
        ({"stdout": "", "stderr": "", "exit_code": 127}, True),
        ({"stdout": "", "stderr": "warning: x", "exit_code": 0}, False),
        ({"content": "fine"}, False),
    ],
)
def test_which_results_count_as_failed_calls(result, failed):
    assert _call_failed(result) is failed


# ── arguments the tool does not take ──────────────────────────────────────────────────────────────────────────────


async def test_an_argument_a_builtin_tool_does_not_take_is_named_in_its_result(tmp_path):
    (tmp_path / "a.txt").write_text("Hello\n")
    call = ToolCall(id="g1", name="grep", arguments={"pattern": "hello", "-i": True, "path": "."})
    provider = Replies([[call]])
    loop = loop_for(tmp_path, provider)
    await drain(loop)
    result = next(m for m in provider.seen[1] if m.role == "tool")
    assert "ignored arguments: -i" in str(result.content) and "grep takes:" in str(result.content)


async def test_a_call_with_only_known_arguments_gets_no_note(tmp_path):
    (tmp_path / "a.txt").write_text("Hello\n")
    provider = Replies([[ToolCall(id="g1", name="grep", arguments={"pattern": "Hello"})]])
    await drain(loop_for(tmp_path, provider))
    assert "ignored arguments" not in str(next(m for m in provider.seen[1] if m.role == "tool").content)


async def test_the_older_start_end_of_read_are_honoured_and_not_reported_as_ignored(tmp_path):
    (tmp_path / "a.txt").write_text("".join(f"line {i}\n" for i in range(1, 8)))
    provider = Replies([[ToolCall(id="r1", name="read", arguments={"path": "a.txt", "start": 3, "end": 4})]])
    await drain(loop_for(tmp_path, provider))
    shown = str(next(m for m in provider.seen[1] if m.role == "tool").content)
    assert "line 3" in shown and "line 4" in shown and "line 5" not in shown
    assert "ignored arguments" not in shown


def test_claude_codes_spellings_are_renamed_not_dropped():
    from k3code.tools.validate import normalize_arguments

    assert normalize_arguments("bash", {"command": "serve", "run_in_background": True}, None) == {
        "command": "serve",
        "background": True,
    }
    assert normalize_arguments("task", {"prompt": "p", "subagent_type": "explorer"}, None) == {
        "prompt": "p",
        "agent_type": "explorer",
    }
    assert normalize_arguments("todo", {"items": [1]}, None) == {"todos": [1]}
    # the real name wins; the alias then stays and is reported as ignored
    both = {"command": "x", "background": False, "run_in_background": True}
    assert normalize_arguments("bash", both, None) == both
    assert normalize_arguments("read", {"path": "a", "run_in_background": True}, None) == {
        "path": "a",
        "run_in_background": True,
    }


async def test_a_bash_call_in_claude_codes_spelling_starts_a_job_instead_of_waiting_for_the_timeout(tmp_path):
    call = ToolCall(id="b1", name="bash", arguments={"command": "sleep 30", "run_in_background": True})
    provider = Replies([[call]])
    loop = loop_for(tmp_path, provider, session="s-alias")
    try:
        await asyncio.wait_for(drain(loop), timeout=20)
        shown = str(next(m for m in provider.seen[1] if m.role == "tool").content)
        assert "in the background" in shown and "ignored arguments" not in shown
    finally:
        from k3code.tools.jobs import reap

        await reap("s-alias")


def test_the_note_is_rendered_after_the_result():
    out = format_tool_result({"matches": "a.txt:1:Hello", IGNORED_KEY: "ignored arguments: -i; grep takes: pattern"})
    assert out == "a.txt:1:Hello\n[ignored arguments: -i; grep takes: pattern]"


# ── bash ──────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs /bin/bash")
async def test_bash_runs_under_bash(tmp_path):
    (tmp_path / "x").write_text("")
    res = await tool_bash(
        {"command": "[[ -f x ]] && echo yes; set -o pipefail; echo ${BASH_VERSION:+bash}"}, cwd=tmp_path
    )
    assert res["exit_code"] == 0 and res["stdout"].split() == ["yes", "bash"]


async def test_a_timeout_above_the_maximum_is_clamped_and_says_so(tmp_path):
    res = await tool_bash({"command": "true", "timeout": 3600}, cwd=tmp_path)
    assert res["exit_code"] == 0 and "timeout 3600 was cut to 600" in res["note"]


def test_sandbox_failures_are_explained():
    from k3code.tools import _sandbox_hint

    assert ".git is read-only" in _sandbox_hint("fatal: could not lock config: Read-only file system", "git commit")
    assert "no network" in _sandbox_hint("curl: (6) Could not resolve host: example.com", "curl example.com")
    # an interactive session's sandbox keeps the network: a typo in a host name is not the sandbox's doing
    assert _sandbox_hint("curl: (6) Could not resolve host: exmaple.com", "curl exmaple.com", network=True) == ""
    assert "private to each bash call" in _sandbox_hint("cat: /tmp/out: No such file or directory", "cat /tmp/out")
    assert _sandbox_hint("cat: out: No such file or directory", "cat out") == ""
    # the command mentions /tmp but the missing file is in the project
    assert _sandbox_hint("cp: cannot stat 'a.txt': No such file or directory", "cp a.txt /tmp/a.txt") == ""
    assert _sandbox_hint("", "true") == ""


needs_bwrap = pytest.mark.skipif(not sandbox.usable(), reason="bubblewrap is unusable here")


@needs_bwrap
async def test_a_sandboxed_background_child_is_reported_as_stopped(tmp_path):
    argv = sandbox.build_argv(tmp_path, [], network=False)
    res = await tool_bash({"command": "sleep 60 & echo started"}, cwd=tmp_path, sandbox=argv)
    assert res["exit_code"] == 0 and "started" in res["stdout"]
    assert "started in the background was stopped" in res["stdout"]


@needs_bwrap
async def test_the_leftover_note_starts_on_its_own_line_and_is_not_traced(tmp_path):
    argv = sandbox.build_argv(tmp_path, [], network=False)
    res = await tool_bash({"command": "sleep 60 & printf started"}, cwd=tmp_path, sandbox=argv)
    assert res["stdout"].startswith("started\n[a process this command started"), res["stdout"]
    traced = await tool_bash({"command": "set -x; sleep 60 & echo hi"}, cwd=tmp_path, sandbox=argv)
    assert "started in the background was stopped" in traced["stdout"]
    assert "/proc/" not in traced["stderr"] and "-gt" not in traced["stderr"], "the trap's own lines are not traced"


@needs_bwrap
async def test_a_sandboxed_command_without_leftovers_prints_nothing_extra_and_keeps_its_exit_code(tmp_path):
    argv = sandbox.build_argv(tmp_path, [], network=False)
    assert (await tool_bash({"command": "echo plain"}, cwd=tmp_path, sandbox=argv))["stdout"] == "plain\n"
    res = await tool_bash({"command": "exit 3"}, cwd=tmp_path, sandbox=argv)
    assert res["exit_code"] == 3 and res["stdout"] == ""
    res = await tool_bash({"command": "sleep 60 & exit 5"}, cwd=tmp_path, sandbox=argv)
    assert res["exit_code"] == 5


@needs_bwrap
async def test_a_sandboxed_git_commit_failure_explains_the_read_only_git_dir(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    argv = sandbox.build_argv(tmp_path, [], network=False)
    res = await tool_bash(
        {"command": "git -c user.name=t -c user.email=t@t commit --allow-empty -qm x"}, cwd=tmp_path, sandbox=argv
    )
    assert res["exit_code"] != 0 and ".git is read-only" in res.get("note", "")


# ── grep / glob ───────────────────────────────────────────────────────────────────────────────────────────────────

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")


@needs_rg
async def test_grep_retries_in_multiline_mode_when_rg_asks_for_it(tmp_path):
    (tmp_path / "a.txt").write_text("first\nsecond\nthird\n")
    res = await tool_grep({"pattern": r"first\nsecond"}, cwd=tmp_path)
    assert "error" not in res and "[searched in multiline mode]" in res["matches"] and "first" in res["matches"]


@needs_rg
@pytest.mark.skipif(
    subprocess.run(["rg", "--pcre2-version"], capture_output=True).returncode != 0 if shutil.which("rg") else True,
    reason="rg without PCRE2",
)
async def test_grep_retries_with_pcre2_for_a_look_around(tmp_path):
    (tmp_path / "a.txt").write_text("foobar\nbar\n")
    res = await tool_grep({"pattern": r"(?<=foo)bar"}, cwd=tmp_path)
    assert "error" not in res and "[searched with PCRE2]" in res["matches"] and "a.txt\n1:foobar" in res["matches"]


@needs_rg
async def test_a_bad_regex_is_still_an_error_with_rgs_own_words(tmp_path):
    (tmp_path / "a.txt").write_text("foo(\n")
    res = await tool_grep({"pattern": "foo("}, cwd=tmp_path)
    assert res["error"].startswith("grep failed:") and "unclosed group" in res["error"]


async def test_glob_paths_are_relative_to_the_session_directory(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text("x = 1\n")
    res = await tool_glob({"pattern": "*.py", "path": "src/pkg"}, cwd=tmp_path)
    assert res["files"] == ["src/pkg/mod.py"], "a path that read takes as it is"
    assert (await tool_glob({"pattern": "*.py", "path": "src"}, cwd=tmp_path))["files"] == ["src/pkg/mod.py"]
    assert "give path=dist" in format_tool_result({"files": []})


# ── tool-call JSON from the model ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("parse", [openai_parse, anthropic_parse])
def test_a_raw_newline_inside_a_string_argument_is_accepted(parse):
    raw = '{"path": "a.py", "content": "line one\nline two\tend"}'
    assert parse(raw) == {"path": "a.py", "content": "line one\nline two\tend"}
    assert json.loads(json.dumps(parse(raw))) == parse(raw)


@pytest.mark.parametrize("parse", [openai_parse, anthropic_parse])
def test_truncated_arguments_stay_unparsed(parse):
    assert parse('{"path": "a.py", "content": "half') == {"_unparsed": '{"path": "a.py", "content": "half'}


def test_the_error_for_broken_arguments_says_where_they_break():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    (problem,) = check_arguments(schema, {"_unparsed": '{"path": "a.py",, "x": 1}'})
    assert "not a valid JSON object: Expecting property name" in problem and "(char " in problem
    (problem,) = check_arguments(schema, {"_raw": ["a.py"]})
    assert problem.startswith("the arguments were not a valid JSON object (received:")


def test_a_progress_bars_redraw_frames_are_collapsed_to_the_last_one():
    bar = "".join(f"\rDownloading {n}%" for n in range(0, 101, 5)) + "\ndone\n"
    shown = format_tool_result({"stdout": bar, "stderr": "", "exit_code": 0})
    assert shown == "Downloading 100%\ndone"
    assert format_tool_result({"stdout": "a\r\nb\r\n", "stderr": "", "exit_code": 0}) == "a\nb"
    assert format_tool_result({"stdout": "plain\n", "stderr": "", "exit_code": 0}) == "plain"


def test_the_reason_for_broken_arguments_is_the_one_the_lenient_parser_hits():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    raw = '{"path": "a.py", "content": "def f():\n    print("hi")\n"}'  # raw newlines (accepted) and unescaped quotes
    (problem,) = check_arguments(schema, {"_unparsed": raw})
    assert "control character" not in problem and "Expecting ',' delimiter" in problem and "at at" not in problem


def test_output_that_is_only_cr_separated_records_says_that_frames_were_dropped():
    csv = format_tool_result({"stdout": "id,name\r1,alice\r2,bob\r3,carol\r", "stderr": "", "exit_code": 0})
    assert csv.startswith("3,carol") and "3 earlier carriage-return frames dropped" in csv and "tr '\\r' '\\n'" in csv
    # a redraw that ends its line is a progress bar: no note
    assert format_tool_result({"stdout": "10%\r50%\r100%\n", "stderr": "", "exit_code": 0}) == "100%"


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
async def test_grep_searches_hidden_directories_but_keeps_git_and_credential_files_out(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("- run: pytest -q\n")
    (tmp_path / ".eslintrc.json").write_text('{"rules": {"no-console": "error"}}\n')
    (tmp_path / ".env").write_text("API_KEY=" + "pytest-dotenv\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n\turl = pytest\n")
    (tmp_path / "app.py").write_text("# pytest is used\n")
    found = (await tool_grep({"pattern": "pytest"}, cwd=tmp_path))["matches"]
    assert ".github/workflows/ci.yml\n1:" in found and "app.py\n1:" in found
    assert ".env" not in found and ".git/" not in found
    assert ".eslintrc.json" in (await tool_grep({"pattern": "no-console"}, cwd=tmp_path))["matches"]
    with_include = (await tool_grep({"pattern": "pytest", "include": "*"}, cwd=tmp_path))["matches"]
    assert ".github/workflows/ci.yml" in with_include and ".env" not in with_include


@pytest.mark.parametrize("path", ["bad\x00.txt", "lone\udc80.txt", "~nosuchuser-zz/file.txt"])
async def test_a_path_the_system_cannot_take_is_refused_without_ending_the_turn(tmp_path, path):
    calls = [ToolCall(id="w1", name="write", arguments={"path": path, "content": "x"})]
    for name in ("read", "grep"):
        calls.append(ToolCall(id=f"{name}1", name=name, arguments={"path": path, "pattern": "x"}))
    provider = Replies([calls])
    loop = loop_for(tmp_path, provider)
    events = await drain(loop)
    results = [str(m.content) for m in provider.seen[1] if m.role == "tool"]
    assert len(results) == 3 and all(r.startswith("Error: invalid arguments for") for r in results), results
    assert events[-1].message.content == "done", "the model answered after seeing the errors"


async def test_write_names_what_is_wrong_with_a_directory_or_a_file_in_the_way(tmp_path):
    from k3code.tools import tool_write

    (tmp_path / "src").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    for args, words in [
        ({"path": "src", "content": "x"}, "is a directory, not a file"),
        ({"path": "notes.txt/inner.txt", "content": "x"}, "is a file, so"),
    ]:
        err = (await tool_write(args, cwd=tmp_path))["error"]
        assert words in err and ".k3tmp" not in err
    assert (await tool_write({"path": "new/dir/f.txt", "content": "ok"}, cwd=tmp_path))["ok"]
    assert (tmp_path / "new" / "dir" / "f.txt").read_text() == "ok"


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
async def test_grep_forms_that_cannot_match_say_so_instead_of_finding_nothing(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def foo():\n    hello = 1\n")
    (tmp_path / "b.js").write_text("function bar() {}\n")
    # a comma-separated include is the several globs it names
    both = (await tool_grep({"pattern": "def foo|function bar", "include": "*.py,*.js"}, cwd=tmp_path))["matches"]
    assert "a.py\n1:" in both and "b.js\n1:" in both
    # BRE alternation, and an include that is no glob: nothing found, with the likely reason
    res = await tool_grep({"pattern": "hello\\|HELLO"}, cwd=tmp_path)
    assert res["matches"] == "" and "literal pipe" in res["warnings"]
    shown = format_tool_result(await tool_grep({"pattern": "foo", "include": "python"}, cwd=tmp_path))
    assert shown.startswith("[no matches. Check: include is a file-name glob")
    # a plain miss stays a plain miss
    assert format_tool_result(await tool_grep({"pattern": "zzz"}, cwd=tmp_path)) == "(no matches)"
    # a glob in path
    err = (await tool_grep({"pattern": "x", "path": "src/*.py"}, cwd=tmp_path))["error"]
    assert "not a glob" in err
    # a non-string include item is named instead of crashing the tool
    schema = {"type": "object", "properties": {"include": {"type": ["string", "array"], "items": {"type": "string"}}}}
    assert check_arguments(schema, {"include": [1, 2]}) == [
        "'include[0]' must be string, got int",
        "'include[1]' must be string, got int",
    ]


async def test_glob_with_a_file_as_its_path_says_so(tmp_path):
    (tmp_path / "a.py").write_text("x")
    err = (await tool_glob({"pattern": "*.py", "path": "a.py"}, cwd=tmp_path))["error"]
    assert "is a file, not a directory" in err


async def test_calls_the_loop_guard_refused_are_answered_so_the_history_is_complete(tmp_path):
    # one identical call per reply, ids differ: the guard refuses the reply that repeats it too often
    provider = Replies([[ToolCall(id=f"d{i}", name="bash", arguments={"command": "echo deploy"})] for i in range(8)])
    loop = loop_for(tmp_path, provider)
    events = await drain(loop)
    assert loop.escalation_reason == "loop_guard", "the guard stopped the turn"
    asked = [tc.id for m in loop.turn_messages if m.role == "assistant" for tc in m.tool_calls]
    answered = [m.tool_call_id for m in loop.turn_messages if m.role == "tool"]
    assert asked == answered, "every call has a result, the refused one too"
    refused = next(m for m in reversed(loop.turn_messages) if m.role == "tool")
    assert refused.content.startswith("Error: not run") and "loop guard" in refused.content
    assert events[-1].message.content.startswith("I stopped because I was repeating myself")


async def test_a_bare_star_lists_the_folder_and_a_cut_listing_keeps_the_shallow_paths(tmp_path):
    for top in ("alpha", "beta", "zeta"):
        (tmp_path / top).mkdir()
        (tmp_path / top / "inner.py").write_text("x")
    (tmp_path / "top.txt").write_text("x")
    listing = (await tool_glob({"pattern": "*"}, cwd=tmp_path))["files"]
    assert sorted(listing) == ["alpha", "beta", "top.txt", "zeta"], "children only, like ls"
    assert "alpha/inner.py" in (await tool_glob({"pattern": "*.py"}, cwd=tmp_path))["files"], "other patterns recurse"
    # past the cap the shallow paths stay (the alphabetical head used to push the last top-level folders out)
    (tmp_path / "alpha" / "deep").mkdir()
    for i in range(1_100):
        (tmp_path / "alpha" / "deep" / f"f{i:04d}.log").write_text("x")
    res = await tool_glob({"pattern": "**/*"}, cwd=tmp_path)
    assert "zeta" in res["files"] and "zeta/inner.py" in res["files"] and "shallowest" in res["note"]


async def test_bash_output_is_cleaned_like_bash_output(tmp_path):
    from k3code.tools import jobs, tool_bash_output

    started = await tool_bash(
        {"command": r"printf '\033[32mgreen\033[0m\n10%%\r50%%\r100%%\ndone\n'", "background": True},
        cwd=tmp_path,
        session_id="s-clean",
    )
    job_id = started["content"].split()[1]
    try:
        for _ in range(50):
            res = await tool_bash_output({"job_id": job_id}, session_id="s-clean")
            if "done" in res["content"]:
                break
            await asyncio.sleep(0.1)
        assert "\x1b" not in res["content"] and "10%" not in res["content"]
        assert "green" in res["content"] and "100%" in res["content"]
    finally:
        await jobs.reap("s-clean")


def test_grep_results_name_each_file_once():
    from k3code.tools import _group_by_file

    lines = ["a.py:3:import os", "a.py:9:os.exit()", "b/c.py:1:os = 1", "12:text of a single-file search"]
    assert _group_by_file(lines).splitlines() == [
        "a.py",
        "3:import os",
        "9:os.exit()",
        "",
        "b/c.py",
        "1:os = 1",
        "12:text of a single-file search",
    ]
    assert _group_by_file([]) == ""


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
async def test_a_search_of_one_file_is_left_as_rg_prints_it(tmp_path):
    (tmp_path / "log.txt").write_text("host:34:up\nplain\n")
    res = await tool_grep({"pattern": "host", "path": "log.txt"}, cwd=tmp_path)
    assert res["matches"] == "1:host:34:up"
