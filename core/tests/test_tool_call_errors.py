"""Tool calls that fail for a reason the model could have been told: what counts toward the stop, what a failure says,
and the small sharp edges of bash, grep, glob, edit and the argument parsers."""

from __future__ import annotations

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
    assert "private to each bash call" in _sandbox_hint("cat: /tmp/out: No such file or directory", "cat /tmp/out")
    assert _sandbox_hint("cat: out: No such file or directory", "cat out") == ""
    assert _sandbox_hint("", "true") == ""


needs_bwrap = pytest.mark.skipif(not sandbox.usable(), reason="bubblewrap is unusable here")


@needs_bwrap
async def test_a_sandboxed_background_child_is_reported_as_stopped(tmp_path):
    argv = sandbox.build_argv(tmp_path, [], network=False)
    res = await tool_bash({"command": "sleep 60 & echo started"}, cwd=tmp_path, sandbox=argv)
    assert res["exit_code"] == 0 and "started" in res["stdout"]
    assert "started in the background was stopped" in res["stdout"]


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
    assert "error" not in res and "[searched with PCRE2]" in res["matches"] and "a.txt:1:foobar" in res["matches"]


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
    assert "not a valid JSON object: Expecting property name" in problem and "at char" in problem
    (problem,) = check_arguments(schema, {"_raw": ["a.py"]})
    assert problem.startswith("the arguments were not a valid JSON object (received:")


def test_a_progress_bars_redraw_frames_are_collapsed_to_the_last_one():
    bar = "".join(f"\rDownloading {n}%" for n in range(0, 101, 5)) + "\ndone\n"
    shown = format_tool_result({"stdout": bar, "stderr": "", "exit_code": 0})
    assert shown == "Downloading 100%\ndone"
    assert format_tool_result({"stdout": "a\r\nb\r\n", "stderr": "", "exit_code": 0}) == "a\nb"
    assert format_tool_result({"stdout": "plain\n", "stderr": "", "exit_code": 0}) == "plain"
