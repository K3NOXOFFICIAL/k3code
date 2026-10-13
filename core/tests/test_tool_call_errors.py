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
    call = ToolCall(id="g1", name="grep", arguments={"pattern": "hello", "multiline": True, "path": "."})
    provider = Replies([[call]])
    loop = loop_for(tmp_path, provider)
    await drain(loop)
    result = next(m for m in provider.seen[1] if m.role == "tool")
    assert "ignored arguments: multiline" in str(result.content) and "grep takes:" in str(result.content)


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


def test_claude_codes_file_path_skill_glob_and_case_flag_are_understood():
    from k3code.tools.validate import normalize_arguments

    assert normalize_arguments("read", {"file_path": "a.py", "offset": 3}, None) == {"path": "a.py", "offset": 3}
    assert normalize_arguments("write", {"file_path": "a.py", "content": "x"}, None) == {"path": "a.py", "content": "x"}
    edit = {"file_path": "a.py", "old_str": "o", "new_str": "n"}
    assert normalize_arguments("edit", edit, None) == {"path": "a.py", "old_string": "o", "new_string": "n"}
    assert normalize_arguments("skill", {"skill": "pdf"}, None) == {"name": "pdf"}
    assert normalize_arguments("grep", {"pattern": "x", "glob": "*.py"}, None) == {"pattern": "x", "include": "*.py"}
    # the real name wins here too
    both = {"path": "b.py", "file_path": "a.py"}
    assert normalize_arguments("read", both, None) == both
    # -i is the case flag: the regex takes it inline, so a search is not run case-sensitively and answered "no matches"
    assert normalize_arguments("grep", {"pattern": "hello", "-i": True}, None) == {"pattern": "(?i)hello"}
    assert normalize_arguments("grep", {"pattern": "(?i)hello", "-i": True}, None) == {"pattern": "(?i)hello"}
    assert normalize_arguments("grep", {"pattern": "hello", "-i": False}, None) == {"pattern": "hello"}
    assert normalize_arguments("bash", {"command": "ls", "-i": True}, None) == {"command": "ls", "-i": True}


async def test_a_read_and_a_case_insensitive_grep_in_claude_codes_spelling_run(tmp_path):
    (tmp_path / "a.txt").write_text("Hello world\n")
    (tmp_path / "b.md").write_text("Hello too\n")
    calls = [
        ToolCall(id="r1", name="read", arguments={"file_path": "a.txt"}),
        ToolCall(id="g1", name="grep", arguments={"pattern": "hello", "-i": True, "glob": "*.txt"}),
    ]
    provider = Replies([calls])
    await drain(loop_for(tmp_path, provider))
    read, grep = (str(m.content) for m in provider.seen[1] if m.role == "tool")
    assert read == "1\tHello world"
    assert grep.startswith("a.txt\n1:Hello world") and "b.md" not in grep and "ignored arguments" not in grep


async def test_a_skill_call_with_the_skill_argument_is_looked_up_by_that_name(tmp_path):
    from k3code.extratools import register_skill_tool

    provider = Replies([[ToolCall(id="s1", name="skill", arguments={"skill": "nosuchskill"})]])
    loop = loop_for(tmp_path, provider)
    register_skill_tool(loop.tools, tmp_path, [])
    await drain(loop)
    shown = str(next(m for m in provider.seen[1] if m.role == "tool").content)
    assert shown.startswith("Error: Unknown skill: nosuchskill") and "ignored arguments" not in shown


def test_every_alias_names_an_argument_its_tool_takes():
    from k3code.extratools import register_skill_tool
    from k3code.tools import build_registry
    from k3code.tools.validate import ARGUMENT_ALIASES

    reg = build_registry()
    register_skill_tool(reg, Path.cwd(), [])
    checked = set()
    for tool, aliases in ARGUMENT_ALIASES.items():
        if (found := reg.get(tool)) is not None:  # `task` is registered with the sub-agents
            assert set(aliases.values()) <= set(found[0].parameters["properties"]), (tool, aliases)
            checked.add(tool)
    assert {"bash", "todo", "read", "write", "edit", "skill", "grep"} <= checked


def test_an_unused_claude_code_option_sent_as_null_is_dropped_not_renamed_into_a_null():
    from k3code.tools import build_registry
    from k3code.tools.validate import invalid_arguments, normalize_arguments

    bash = build_registry().get("bash")[0].parameters
    args = normalize_arguments("bash", {"command": "echo hi", "run_in_background": None}, bash)
    assert args == {"command": "echo hi"} and invalid_arguments("bash", bash, args) is None
    task = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string"}},
        "required": ["prompt"],
    }
    args = normalize_arguments("task", {"prompt": "p", "subagent_type": None}, task)
    assert args == {"prompt": "p"} and invalid_arguments("task", task, args) is None
    # a null next to the other spelling of the same option: the value wins whichever name carries it
    both = {"command": "x", "background": True, "run_in_background": None}
    assert normalize_arguments("bash", both, bash) == {"command": "x", "background": True}
    both = {"command": "x", "background": None, "run_in_background": True}
    assert normalize_arguments("bash", both, bash) == {"command": "x", "background": True}


async def test_a_bash_call_that_sends_claude_codes_background_option_as_null_runs(tmp_path):
    call = ToolCall(id="b1", name="bash", arguments={"command": "echo hi", "run_in_background": None})
    provider = Replies([[call]])
    await drain(loop_for(tmp_path, provider))
    shown = str(next(m for m in provider.seen[1] if m.role == "tool").content)
    assert shown.startswith("hi") and "invalid arguments" not in shown


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


def test_the_private_tmp_hint_is_not_given_for_a_file_missing_inside_the_project(monkeypatch):
    from k3code.tools import _sandbox_hint

    monkeypatch.setenv("HOME", "/home/someone")
    home_err = "cat: /home/someone/proj/missing.txt: No such file or directory"
    # the project lies under the home: a missing file in it is missing, not discarded
    assert _sandbox_hint(home_err, "cat x", roots=["/home/someone/proj"]) == ""
    assert "private to each bash call" in _sandbox_hint(home_err, "cat x")  # no roots given: as before
    # a file outside the kept folders, and a sibling whose name only starts like the project's
    assert "private to each bash call" in _sandbox_hint(
        home_err.replace("/proj/", "/other/"), "cat x", roots=["/home/someone/proj"]
    )
    assert "private to each bash call" in _sandbox_hint(
        home_err.replace("/proj/", "/proj2/"), "cat x", roots=["/home/someone/proj"]
    )
    # the same for a project under /tmp, with the path in quotes
    tmp_err = "python3: can't open file '/tmp/proj/nope.py': [Errno 2] No such file or directory"
    assert _sandbox_hint(tmp_err, "python3 /tmp/proj/nope.py", roots=[Path("/tmp/proj")]) == ""
    assert "private to each bash call" in _sandbox_hint(tmp_err, "python3 /tmp/proj/nope.py")


needs_bwrap = pytest.mark.skipif(not sandbox.usable(), reason="bubblewrap is unusable here")


@needs_bwrap
async def test_a_missing_file_in_a_project_under_the_home_gets_no_private_tmp_hint(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    argv = sandbox.build_argv(proj, [], network=False)
    res = await tool_bash({"command": f"cat {proj}/missing.txt"}, cwd=proj, sandbox=argv)
    assert res["exit_code"] != 0 and "No such file" in res["stderr"]
    assert "private to each bash call" not in format_tool_result(res)
    # a file the model parked in the (discarded) home outside the project still gets the explanation
    gone = await tool_bash({"command": f"cat {home}/notes.txt"}, cwd=proj, sandbox=argv)
    assert "private to each bash call" in format_tool_result(gone)


@needs_bwrap
async def test_the_private_tmp_hint_is_given_when_the_call_itself_runs_in_tmp_or_the_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    argv = sandbox.build_argv(proj, [], network=False)
    # the cwd of a call is not a folder the sandbox keeps: inside it /tmp and the home are the private tmpfs
    for cwd, missing in (("/tmp", "/tmp/notes.txt"), (str(home), f"{home}/notes.txt")):
        res = await tool_bash({"command": f"cat {missing}", "cwd": cwd}, cwd=proj, sandbox=argv)
        assert res["exit_code"] != 0 and "No such file" in res["stderr"], (cwd, res)
        assert "private to each bash call" in format_tool_result(res), cwd
    # and a file missing inside the project is still just missing, whatever the cwd
    res = await tool_bash({"command": f"cat {proj}/nope.txt", "cwd": "/tmp"}, cwd=proj, sandbox=argv)
    assert "private to each bash call" not in format_tool_result(res)


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
    skipped = format_tool_result(await tool_glob({"pattern": "dist/*.js"}, cwd=tmp_path))
    assert skipped.startswith("(no files)") and "give path=dist" in skipped


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


def test_a_progress_bar_above_a_line_that_only_clears_the_spinner_is_not_cr_separated_data():
    from k3code.tools import _collapse_redraws

    # the usual end of a spinner: its line is cleared with a bare CR and an erase code, after a bar that ended its line
    shown = format_tool_result({"stdout": "10%\r50%\r100%\nDone\r\x1b[K", "stderr": "", "exit_code": 0})
    assert shown == "100%\nDone"
    for text in ("10%\r50%\r100%\ndone\r", "5%\r10%\r\ndone\r"):
        assert "carriage-return" not in _collapse_redraws(text), text
    # a last line that is itself CR-separated still says so, with the earlier frames counted in
    assert "4 earlier carriage-return frames dropped" in _collapse_redraws("10%\r50%\nid\r1,a\r2,b\r3,c\r")


async def test_cr_separated_records_say_that_frames_were_dropped_after_other_output_too(tmp_path):
    from k3code.tools import _collapse_redraws, jobs, tool_bash_output

    note = "3 earlier carriage-return frames dropped"
    assert note in _collapse_redraws("[job1 exited with code 0]\nid\r1,a\r2,b\r3,c\r")
    # a progress bar that ends its line, with or without text before it, is no data
    assert _collapse_redraws("head\n10%\r50%\r100%\n") == "head\n100%\n"
    records = r"id,name\r1,alice\r2,bob\r3,carol\r"
    res = await tool_bash({"command": f"echo header; printf '{records}'"}, cwd=tmp_path)
    shown = format_tool_result(res)
    assert shown.startswith("header\n3,carol") and note in shown
    started = await tool_bash({"command": f"printf '{records}'", "background": True}, cwd=tmp_path, session_id="s-cr")
    try:
        for _ in range(50):
            out = await tool_bash_output({"job_id": started["content"].split()[1]}, session_id="s-cr")
            if "carol" in out["content"]:
                break
            await asyncio.sleep(0.1)
        assert "3,carol" in out["content"] and note in out["content"]
    finally:
        await jobs.reap("s-cr")


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


def test_the_no_match_hints_name_the_slips_and_stay_silent_for_a_correct_search():
    from k3code.tools import _no_match_hints

    # a file glob and a deliberate pair of literal pipes: nothing to check
    assert _no_match_hints("zzz", "*.py") == ""
    assert _no_match_hints("zzz", ["*.py", "Cargo.toml", ".env*"]) == ""
    assert _no_match_hints(r"\|\|", ["*.py"]) == ""
    assert _no_match_hints("zzz", None) == ""
    # the slips: a folder or a language as include, grep's `\|` between two words
    assert "file-name glob" in _no_match_hints("zzz", "src")
    assert "file-name glob" in _no_match_hints("zzz", ["*.py", "python"])
    assert "literal pipe" in _no_match_hints(r"foo\|bar", None)
    both = _no_match_hints(r"foo\|bar", "src")
    assert "literal pipe" in both and "file-name glob" in both


def test_the_alternation_hint_covers_the_escapes_and_spaces_around_the_pipe_too():
    from k3code.tools import _no_match_hints

    for slip in (r"\.js\|\.ts", r"\bfoo\b\|\bbar\b", r"(a\|\(b\))", r"foo \| bar", r"error\|\w+", r"a\|b\|c"):
        assert "literal pipe" in _no_match_hints(slip, None), slip
    # a literal pair of pipes, and an escaped backslash before a real pipe, are not the slip
    for fine in (r"\|\|", r"a\|\|b", r"\\|b", "a|b"):
        assert _no_match_hints(fine, None) == "", fine
    # a brace group is a file glob, so it is not "a folder or a language"
    assert _no_match_hints("zzz", ["*.{c,h}", "foo.{c,h}"]) == ""


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
async def test_a_search_with_escapes_around_a_backslash_pipe_that_finds_nothing_says_why(tmp_path):
    (tmp_path / "a.js").write_text('import x from "./a.js"\n')
    shown = format_tool_result(await tool_grep({"pattern": r"\bimport\b\|\bexport\b"}, cwd=tmp_path))
    assert shown.startswith("[no matches. Check: \\| is a literal pipe")
    assert format_tool_result(await tool_grep({"pattern": r"\bimport\b|\bexport\b"}, cwd=tmp_path)).startswith("a.js")


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
async def test_grep_skips_the_caches_of_common_toolchains_unless_searched_on_purpose(tmp_path):
    cache_dirs = (".next", ".nuxt", ".nox", ".gradle", ".terraform", ".turbo", ".svelte-kit", ".parcel-cache")
    for name in cache_dirs:
        (tmp_path / name).mkdir()
        (tmp_path / name / "gen.js").write_text("needle\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n")
    assert (await tool_grep({"pattern": "needle"}, cwd=tmp_path))["matches"] == "src/app.py\n1:needle"
    assert (await tool_glob({"pattern": "*.js"}, cwd=tmp_path))["files"] == []
    assert "gen.js" in (await tool_grep({"pattern": "needle", "path": ".next"}, cwd=tmp_path))["matches"]


def test_a_non_string_include_item_is_named_instead_of_crashing_the_tool():
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

    lines = ["a.py\x003:import os", "a.py\x009:os.exit()", "b/c.py\x001:os = 1", "12:text of a single-file search"]
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


def test_grouping_cuts_a_path_at_its_end_not_at_the_first_colon_and_number():
    from k3code.tools import _group_by_file

    log = "logs/2024-01-01T10:30:00.log"
    assert _group_by_file([f"{log}\x002:ERROR boom", f"{log}\x007:ERROR again"]).splitlines() == [
        log,
        "2:ERROR boom",
        "7:ERROR again",
    ]


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
async def test_grep_names_a_file_whose_name_holds_a_time_stamp(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "2024-01-01T10:30:00.log").write_text("ok\nERROR boom\n")
    res = await tool_grep({"pattern": "ERROR"}, cwd=tmp_path)
    assert res["matches"] == "logs/2024-01-01T10:30:00.log\n2:ERROR boom"


async def test_the_python_fallback_names_a_file_whose_name_holds_a_time_stamp(tmp_path):
    from k3code.tools import _grep_fallback

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "2024-01-01T10:30:00.log").write_text("ok\nERROR boom\n")
    res = _grep_fallback("ERROR", tmp_path, None, None, tmp_path)
    assert res["matches"] == "logs/2024-01-01T10:30:00.log\n2:ERROR boom"


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
async def test_a_search_of_one_file_is_left_as_rg_prints_it(tmp_path):
    (tmp_path / "log.txt").write_text("host:34:up\nplain\n")
    res = await tool_grep({"pattern": "host", "path": "log.txt"}, cwd=tmp_path)
    assert res["matches"] == "1:host:34:up"


def test_the_redraw_collapse_covers_stderr_and_the_ignored_note_exempts_what_it_must():
    from types import SimpleNamespace

    from k3code.agent.loop import _ignored_arguments

    shown = format_tool_result({"stdout": "", "stderr": "10%\r50%\r100%\ndone\n", "exit_code": 1})
    assert "[stderr]\n100%\ndone" in shown and "50%" not in shown
    read = SimpleNamespace(name="read", parameters={"properties": {"path": {}, "offset": {}}})
    assert _ignored_arguments(read, {"path": "a", "_unparsed": "x"}) == "", "provider markers are not arguments"
    assert "extra" in _ignored_arguments(read, {"path": "a", "extra": 2})
    mcp = SimpleNamespace(name="mcp__s__t", parameters={"properties": {"a": {}}})
    assert _ignored_arguments(mcp, {"a": 1, "extra": 2}) == "", "an MCP server may take more than it declares"


def test_a_label_argument_on_bash_is_dropped_without_a_note_and_other_unknowns_are_still_named():
    from types import SimpleNamespace

    from k3code.agent.loop import _ignored_arguments

    props = {"command": {}, "timeout": {}}
    bash = SimpleNamespace(name="bash", parameters={"properties": props})
    assert _ignored_arguments(bash, {"command": "ls", "description": "List files"}) == ""
    note = _ignored_arguments(bash, {"command": "ls", "description": "List files", "shell": "zsh"})
    assert note.startswith("ignored arguments: shell;") and "description" not in note.split(";")[0]
    # only bash's label is exempt: another tool's unknown `description` is a dropped argument
    grep = SimpleNamespace(name="grep", parameters={"properties": {"pattern": {}}})
    assert _ignored_arguments(grep, {"pattern": "x", "description": "d"}).startswith("ignored arguments: description;")


async def test_a_bash_call_with_a_description_label_comes_back_as_just_its_output(tmp_path):
    call = ToolCall(id="b1", name="bash", arguments={"command": "echo hi", "description": "Print hi"})
    provider = Replies([[call]])
    await drain(loop_for(tmp_path, provider))
    assert str(next(m for m in provider.seen[1] if m.role == "tool").content) == "hi"


async def test_the_memory_of_applied_edits_stays_bounded(tmp_path, monkeypatch):
    from k3code import tools as tools_mod
    from k3code.tools import tool_edit

    monkeypatch.setattr(tools_mod, "_APPLIED_EDITS", {})
    for i in range(tools_mod._APPLIED_EDITS_MAX + 44):
        f = tmp_path / f"f{i}.txt"
        f.write_text("old\n")
        assert (await tool_edit({"path": f.name, "old_string": "old", "new_string": "new"}, cwd=tmp_path))["ok"]
    assert len(tools_mod._APPLIED_EDITS) <= tools_mod._APPLIED_EDITS_MAX
