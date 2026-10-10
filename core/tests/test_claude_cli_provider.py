"""The claude-cli provider: built-in tools off, plain text + a final tool block; one-shot and persistent processes.

A shim script stands in for the `claude` binary: it records its argv, stdin, cwd and a few env
variables, then prints the stream-json events of a canned reply chosen by the SHIM_MODE variable.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from k3code.config import ProviderEntry
from k3code.doctor import check_keys
from k3code.providers import ClaudeCliProvider, make_providers
from k3code.providers.base import ProviderError
from k3code.providers.claude_cli import _parse_tool_calls, render_prompt
from k3code.providers.types import INVALID_TOOL_CALL, Message, ToolCall, ToolSpec

SHIM = r"""#!{python}
import json, os, sys, time
log = os.environ["SHIM_LOG"]
sysfile = sys.argv[sys.argv.index("--system-prompt-file") + 1]
with open(log, "w") as fh:
    json.dump({{
        "argv": sys.argv[1:],
        "stdin": sys.stdin.read(),
        "cwd": os.getcwd(),
        "system": open(sysfile).read(),
        "env": {{k: os.environ.get(k) for k in (
            "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "OMNIROUTE_API_KEY", "HOME", "MAX_THINKING_TOKENS")}},
    }}, fh)
mode = os.environ.get("SHIM_MODE", "text")
usage = {{"input_tokens": 3, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50, "output_tokens": 7}}
COST = 0.0125
CALLS = '[{{"name": "read_file", "arguments": {{"path": "a.py"}}}}, {{"name": "nope", "arguments": {{}}}}]'
replies = {{
    "text": "hello",
    "long": "line one\n```\n[ todo ]\n```\nRisks:\n- a\n- b\n- c",
    "tool": "Let me read it.\n<tool_calls>" + CALLS + "</tool_calls>",
    "fenced": "<tool_calls>\n```json\n" + CALLS + "\n```\n</tool_calls>",
    "twice": "<tool_calls>[]</tool_calls> oops <tool_calls>" + CALLS + "</tool_calls>",
    "bad": "<tool_calls>[{{not json</tool_calls>",
    "cut": "start <tool_calls>[{{\"name\": \"read_file\"",
    "huge": "x" * 300_000,  # one stdout line far beyond a StreamReader's 64 KiB line limit
}}
replies.update(json.loads(os.environ.get("SHIM_REPLIES", "{{}}")))

def emit(obj):
    print(json.dumps(obj), flush=True)  # a pipe is block-buffered: without the flush nothing streams

def delta(text, kind="text_delta", key="text"):
    emit({{"type": "stream_event", "event": {{"type": "content_block_delta", "index": 0,
                                             "delta": {{"type": kind, key: text}}}}}})

def result(**fields):
    emit({{"type": "result", "subtype": "success", "usage": usage, "total_cost_usd": COST, **fields}})

if mode in replies:
    emit({{"type": "system", "subtype": "init", "tools": []}})
    emit({{"type": "stream_event", "event": {{"type": "message_start", "message": {{}}}}}})
    delta("pondering", "thinking_delta", "thinking")  # thinking is never shown
    open(log + ".reply", "w").write(replies[mode])
    split = os.environ.get("SHIM_SPLIT")  # "chars": one delta per character
    pieces = json.loads(os.environ.get("SHIM_DELTAS", "null")) or (list(replies[mode]) if split else [replies[mode]])
    for piece in pieces:
        delta(piece)
    gate = os.environ.get("SHIM_GATE")
    if gate:  # hold the reply open until the test has seen the first delta
        for _ in range(500):
            if os.path.exists(gate):
                break
            time.sleep(0.02)
    emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": replies[mode]}}]}}}})
    emit({{"type": "rate_limit_event", "rate_limit_info": {{"status": "allowed"}}}})
    result(is_error=False, result=replies[mode])
elif mode == "login":
    result(is_error=True, result="Not logged in · Please run /login"); sys.exit(1)
elif mode == "limit":
    result(is_error=True, result="5-hour limit reached. Resets at 21:00"); sys.exit(1)
elif mode == "status":
    result(is_error=True, result="boom", api_error_status=503); sys.exit(1)
elif mode == "crash":
    sys.stderr.write("segfault-ish"); sys.exit(3)
elif mode == "sleep":
    time.sleep(30)
"""

TOOLS = [
    ToolSpec(
        name="read_file",
        description="Read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    ),
    ToolSpec(
        name="bash",
        description="Run a command",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
    ),
]


@pytest.fixture
def shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "claude-shim"
    path.write_text(SHIM.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("SHIM_LOG", str(tmp_path / "call.json"))
    return path


async def _collect(
    provider: ClaudeCliProvider, messages: list[Message], tools: list[ToolSpec] | None = None, model: str = "m1"
):
    return [e async for e in provider.stream(messages, TOOLS if tools is None else tools, model)]


def _call(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "call.json").read_text())


async def test_text_reply_and_usage(shim: Path, tmp_path: Path) -> None:
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="hi")])
    assert [e.type for e in events] == ["text_delta", "done"]
    assert events[0].text == "hello"
    done = events[-1]
    assert done.message.content == "hello" and not done.message.tool_calls
    assert done.usage.prompt_tokens == 153 and done.usage.completion_tokens == 7  # input + cache read + cache write
    assert done.usage.cost_usd == 0.0125  # Claude Code's own list-price figure, recorded in /stats
    assert done.usage.cache_read_tokens == 100 and done.usage.cache_creation_tokens == 50
    await p.aclose()


async def test_tool_calls_become_k3code_tool_calls(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_MODE", "tool")
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="read a.py")])
    assert [e.type for e in events] == ["text_delta", "tool_call", "tool_call", "done"]
    assert events[0].text == "Let me read it."  # prose kept, the block removed
    calls = [e.tool_call for e in events if e.type == "tool_call"]
    # an unknown name ("nope") is passed on: the agent loop answers "Unknown tool" and the model corrects itself
    assert [(c.name, c.arguments) for c in calls] == [("read_file", {"path": "a.py"}), ("nope", {})]
    assert calls[0].id.startswith("call_") and calls[0].id != calls[1].id
    assert events[-1].message.tool_calls[0].id == calls[0].id
    await p.aclose()


@pytest.mark.parametrize("mode", ["fenced", "twice"])
async def test_tolerant_block_forms(shim: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("SHIM_MODE", mode)  # fenced JSON inside the block; two blocks -> the last one wins
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="x")])
    assert [e.tool_call.name for e in events if e.type == "tool_call"] == ["read_file", "nope"]
    await p.aclose()


async def test_long_reply_is_returned_whole_without_tools(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_MODE", "long")  # /preview-style answer: nothing may be lost or summarised
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="x")], tools=[])
    assert events[0].text.endswith("- c") and "[ todo ]" in events[0].text
    await p.aclose()


async def test_cut_off_tool_block_is_a_provider_error(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_MODE", "cut")  # opened, never closed: the reply was cut off, a retry may help
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    with pytest.raises(ProviderError) as ei:
        await _collect(p, [Message(role="user", content="x")])
    assert ei.value.status_code == 502 and "tool_calls" in ei.value.message
    await p.aclose()


async def test_invalid_tool_block_becomes_one_invalid_tool_call(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_MODE", "bad")  # no ProviderError: the loop tells the model instead of a full retry
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="x")])
    calls = [e.tool_call for e in events if e.type == "tool_call"]
    assert [c.name for c in calls] == [INVALID_TOOL_CALL]
    assert calls[0].arguments["error"] and calls[0].arguments["body"] == "[{not json"
    assert events[-1].message.tool_calls[0].name == INVALID_TOOL_CALL
    await p.aclose()


_READ = {"name": "read_file", "arguments": {"path": "a.py"}}


@pytest.mark.parametrize(
    ("body", "arguments"),
    [
        ('[{"name": "read_file", "arguments": {"path": "a.py",},},]', {"path": "a.py"}),  # trailing commas
        ('[{"name": "read_file", "arguments": {"path": "a.py"}} ,\n ]', {"path": "a.py"}),
        (
            '[{"name": "read_file", "arguments": {"n": None, "a": True, "b": False}}]',
            {"n": None, "a": True, "b": False},
        ),
        ('[{"name": "read_file", "arguments": {"path": "a.py"}}', {"path": "a.py"}),  # missing final ]
        ('[{"name": "read_file", "arguments": {"path": "a.py"}},', {"path": "a.py"}),  # missing ] after a comma
        ('[{"name": "read_file", "arguments": {"path": "x, ] True None \\" }"}},]', {"path": 'x, ] True None " }'}),
    ],
)
def test_lenient_repair(body: str, arguments: dict) -> None:
    calls = _parse_tool_calls(body)
    assert [(c.name, c.arguments) for c in calls] == [("read_file", arguments)]


@pytest.mark.parametrize(
    "body",
    [
        "[{not json",
        '[{"name": "read_file", "arguments": {"path": ]',  # a missing } is not repaired
        "[{'name': 'read_file'}]",
        '[{"name": "a"}] [{"name": "b"}]',
    ],
)
def test_unrepairable_body_is_one_invalid_tool_call(body: str) -> None:
    calls = _parse_tool_calls(body + " " + "x" * 400)
    assert [c.name for c in calls] == [INVALID_TOOL_CALL]
    assert calls[0].arguments["error"] and len(calls[0].arguments["body"]) == 300
    assert json.loads(calls[0].raw_arguments) == calls[0].arguments


async def test_isolation_flags_env_and_cwd(shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://relay.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "secret-key")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    await _collect(
        p, [Message(role="system", content="SYS RULES"), Message(role="user", content="hi")], model="claude-sonnet-5-5"
    )
    seen = _call(tmp_path)
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--tools") + 1] == ""  # Claude Code's own tools are off
    for flag in ("-p", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--json-schema" not in argv  # plain-text protocol: one model turn per call
    assert argv[argv.index("--output-format") + 1] == "stream-json"  # the reply streams
    assert "--verbose" in argv and "--include-partial-messages" in argv
    assert "--effort" not in argv  # neither /effort nor a provider default
    assert seen["system"] == "SYS RULES"
    assert seen["env"]["ANTHROPIC_BASE_URL"] is None and seen["env"]["ANTHROPIC_AUTH_TOKEN"] is None
    assert seen["env"]["OMNIROUTE_API_KEY"] is None
    assert seen["env"]["HOME"] == pwd.getpwuid(os.getuid()).pw_dir  # the login home, not the overridden $HOME
    assert "k3code-claude-cli." in seen["cwd"]  # private empty dir, never the project
    await p.aclose()
    assert not Path(seen["cwd"]).exists()


async def test_prompt_carries_tools_history_and_tool_results(shim: Path, tmp_path: Path) -> None:
    messages = [
        Message(role="user", content="read a.py"),
        Message(
            role="assistant", content=None, tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})]
        ),
        Message(role="tool", content="print(1)", tool_call_id="c1", name="read_file"),
    ]
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    await _collect(p, messages)
    stdin = _call(tmp_path)["stdin"]
    assert "read_file" in stdin and "Run a command" in stdin  # tool catalogue
    assert "unrelated scratch sandbox" in stdin  # the host's own cwd must not be taken for the project
    assert "(called tool read_file id=c1" in stdin
    assert "[tool result id=c1 tool=read_file]\nprint(1)" in stdin
    await p.aclose()


def test_no_tools_prompt_has_no_tool_protocol() -> None:
    system, prompt = render_prompt([Message(role="system", content="S"), Message(role="user", content="u")], [])
    assert system == "S" and "Tools you can call" not in prompt and "<tool_calls>" not in prompt
    _, with_tools = render_prompt([Message(role="user", content="u")], TOOLS)
    assert "<tool_calls>" in with_tools and "Tools you can call" in with_tools


def test_a_mid_turn_system_note_stays_in_the_transcript_not_the_system_prompt() -> None:
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
    head = [
        Message(role="system", content="S"),
        Message(role="user", content="u"),
        Message(role="assistant", content="", tool_calls=[call]),
        Message(role="tool", content="print(1)", tool_call_id="c1", name="read_file"),
    ]
    before, _ = render_prompt(head, TOOLS)
    system, prompt = render_prompt([*head, Message(role="system", content="NOTE: stop repeating")], TOOLS)
    assert system == before == "S"  # the system prompt (the cached prefix) does not change mid-turn
    note = "<system-reminder>\nNOTE: stop repeating\n</system-reminder>"
    assert prompt.index("[tool result id=c1") < prompt.index(note)


@pytest.mark.parametrize(
    ("mode", "status", "needle"),
    [
        ("login", 401, "Not logged in"),
        ("limit", 429, "limit reached"),
        ("status", 503, "boom"),
        ("crash", 502, "segfault"),
    ],
)
async def test_errors_map_to_provider_errors(
    shim: Path, monkeypatch: pytest.MonkeyPatch, mode: str, status: int, needle: str
) -> None:
    monkeypatch.setenv("SHIM_MODE", mode)
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    with pytest.raises(ProviderError) as ei:
        await _collect(p, [Message(role="user", content="x")])
    assert ei.value.status_code == status and needle in ei.value.message
    await p.aclose()


async def test_missing_binary_is_an_auth_style_error(tmp_path: Path) -> None:
    p = ClaudeCliProvider(name="cc", command=str(tmp_path / "does-not-exist"))
    with pytest.raises(ProviderError) as ei:
        await _collect(p, [Message(role="user", content="x")])
    assert ei.value.status_code == 401 and "not found" in ei.value.message
    await p.aclose()


async def test_timeout_kills_the_process(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_MODE", "sleep")
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim), timeout=0.5)
    with pytest.raises(ProviderError) as ei:
        await _collect(p, [Message(role="user", content="x")])
    assert ei.value.status_code == 504
    await p.aclose()


async def test_cancel_kills_and_reaps_the_process(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On cancel the process group was killed but never waited for: a zombie and its pipes were left behind."""
    import asyncio

    procs = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*a, **kw):
        procs.append(await real_exec(*a, **kw))
        return procs[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    monkeypatch.setenv("SHIM_MODE", "sleep")
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim), timeout=30)
    task = asyncio.create_task(_collect(p, [Message(role="user", content="x")]))
    for _ in range(100):
        if procs:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert procs and procs[0].returncode is not None  # killed and reaped before the cancel propagated
    await p.aclose()


def test_config_and_factory_accept_claude_cli() -> None:
    entry = ProviderEntry(name="cc", kind="claude-cli", models={"default": "claude-sonnet-5-5"})
    assert entry.base_url == "" and entry.api_key_env == ""
    (provider,) = make_providers([entry])
    assert isinstance(provider, ClaudeCliProvider) and provider.name == "cc"


def test_api_kinds_still_require_endpoint_and_key_var() -> None:
    with pytest.raises(ValidationError):
        ProviderEntry(name="x", kind="openai", models={})
    with pytest.raises(ValidationError):
        ProviderEntry(name="x", kind="bogus", base_url="u", api_key_env="K")


def test_doctor_does_not_ask_for_a_key_for_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code.config import Settings

    cfg = Settings(providers=[ProviderEntry(name="cc", kind="claude-cli", models={"default": "m"})])
    assert check_keys(cfg).status == "ok"


async def test_hidden_thinking_is_off_for_haiku_only_and_configurable(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Left at Claude Code's default, Haiku 4.5 emitted ~1,570 output tokens for a one-line task (81 with thinking
    off), which ate most of the saving from routing unattended work to the cheap tier. The strong tier keeps its
    thinking (plans, reviews and the advisor use it)."""
    monkeypatch.setenv("MAX_THINKING_TOKENS", "31999")  # an inherited value must never leak into the call
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    await _collect(p, [Message(role="user", content="hi")], model="claude-haiku-4-5-20251001")
    assert _call(tmp_path)["env"]["MAX_THINKING_TOKENS"] == "0"
    await _collect(p, [Message(role="user", content="hi")], model="claude-sonnet-5-5")
    assert _call(tmp_path)["env"]["MAX_THINKING_TOKENS"] is None  # Claude Code's own default
    await p.aclose()
    p = ClaudeCliProvider(
        name="cc", persistent=False, command=str(shim), thinking_tokens=2000, thinking_models=["sonnet"]
    )
    await _collect(p, [Message(role="user", content="hi")], model="claude-sonnet-5-5")
    assert _call(tmp_path)["env"]["MAX_THINKING_TOKENS"] == "2000"
    await _collect(p, [Message(role="user", content="hi")], model="claude-haiku-4-5-20251001")
    assert _call(tmp_path)["env"]["MAX_THINKING_TOKENS"] is None
    await p.aclose()


def test_provider_entry_passes_the_thinking_budget_through() -> None:
    entry = ProviderEntry(name="cc", kind="claude-cli", thinking_tokens=500)
    (provider,) = make_providers([entry])
    assert provider.thinking_tokens == 500 and provider.thinking_models == ("haiku",)
    assert ProviderEntry(name="cc", kind="claude-cli").thinking_tokens == 0


def _reply(result_text: str):
    from k3code.providers.claude_cli import _reply_from_result

    return _reply_from_result({"result": result_text}, TOOLS)


def test_every_tool_block_counts_not_just_the_last() -> None:
    """Haiku 4.5 splits calls over several blocks; keeping only the last dropped the `write` and the task stalled."""
    block = '<tool_calls>[{"name": "read_file", "arguments": {"path": "%s"}}]</tool_calls>'
    text, calls = _reply(
        f"I'll create it.\n{block % 'sq.py'}\nNow let me run it:\n{block % 'b.py'}\nDone! It printed 49."
    )
    assert [c.arguments["path"] for c in calls] == ["sq.py", "b.py"]
    assert text == "I'll create it."  # the narration after the block was written before anything ran: not kept


def test_flattened_arguments_are_accepted() -> None:
    """`{"name": "bash", "command": "ls"}` (arguments next to the name) used to become an empty argument dict, i.e. a
    guaranteed tool error and a stalled task."""
    _, calls = _reply(
        '<tool_calls>[{"name": "read_file", "path": "a.py"}, {"name": "x", "parameters": {"k": 1}},'
        ' {"tool": "y", "input": {"z": 2}}, {"name": "w", "args": "{\\"q\\": 3}"}]</tool_calls>'
    )
    assert [(c.name, c.arguments) for c in calls] == [
        ("read_file", {"path": "a.py"}),
        ("x", {"k": 1}),
        ("y", {"z": 2}),
        ("w", {"q": 3}),
    ]


def test_an_empty_block_keeps_the_text_around_it() -> None:
    text, calls = _reply("before <tool_calls>[]</tool_calls> after")
    assert calls == [] and text == "before  after"


# ── streaming ──


def _spy_procs(monkeypatch: pytest.MonkeyPatch) -> list:
    import asyncio

    procs: list = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*a, **kw):
        procs.append(await real_exec(*a, **kw))
        return procs[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return procs


def _streamed(events) -> str:
    return "".join(e.text for e in events if e.type == "text_delta")


async def test_text_deltas_arrive_while_the_process_still_runs(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --output-format json the whole reply came at once, after 7-11 s of "thinking"."""
    procs = _spy_procs(monkeypatch)
    gate = tmp_path / "gate"
    monkeypatch.setenv("SHIM_GATE", str(gate))  # the shim does not finish the reply before this file exists
    monkeypatch.setenv("SHIM_DELTAS", json.dumps(["hel", "lo"]))
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    stream = p.stream([Message(role="user", content="hi")], [], "m1")
    first = await anext(stream)
    assert first.type == "text_delta" and first.text == "hel"
    assert procs[0].returncode is None and not gate.exists()  # streamed before the CLI finished
    gate.touch()
    rest = [e async for e in stream]
    assert _streamed([first, *rest]) == "hello" == rest[-1].message.content
    await p.aclose()


async def test_closing_the_stream_early_kills_and_reaps_the_process(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs = _spy_procs(monkeypatch)
    monkeypatch.setenv("SHIM_GATE", str(tmp_path / "never"))
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim), timeout=30)
    stream = p.stream([Message(role="user", content="hi")], [], "m1")
    assert (await anext(stream)).type == "text_delta"
    await stream.aclose()
    assert procs[0].returncode is not None
    await p.aclose()


_TOOL_REPLY = (
    'Let me read it.\n<tool_calls>[{"name": "read_file", "arguments": {"path": "a.py"}}, '
    '{"name": "nope", "arguments": {}}]</tool_calls>'
)
_TAG_AT = _TOOL_REPLY.index("<tool_calls>")


@pytest.mark.parametrize("split", [*range(_TAG_AT - 1, _TAG_AT + len("<tool_calls>") + 2), "chars"])
async def test_no_part_of_the_tool_block_is_ever_streamed(
    shim: Path, monkeypatch: pytest.MonkeyPatch, split: int | str
) -> None:
    monkeypatch.setenv("SHIM_MODE", "tool")
    if split == "chars":
        monkeypatch.setenv("SHIM_SPLIT", "chars")
    else:
        monkeypatch.setenv("SHIM_DELTAS", json.dumps([_TOOL_REPLY[:split], _TOOL_REPLY[split:]]))
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="read a.py")])
    assert not any("<" in e.text for e in events if e.type == "text_delta")  # not even "<tool_c"
    done = events[-1]
    assert _streamed(events) == "Let me read it." == done.message.content
    assert [c.name for c in done.message.tool_calls] == ["read_file", "nope"]
    await p.aclose()


@pytest.mark.parametrize("mode", ["text", "long", "tool", "fenced", "twice"])
@pytest.mark.parametrize("tools", [TOOLS, []], ids=["tools", "no-tools"])
async def test_the_final_message_is_what_the_full_text_parses_to(
    shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, tools: list[ToolSpec]
) -> None:
    """Streaming character by character: the done message equals the old one-shot parse of the result text, and the
    streamed text equals its content (the screen shows exactly what is stored)."""
    from k3code.providers.claude_cli import _reply_from_result

    monkeypatch.setenv("SHIM_MODE", mode)
    monkeypatch.setenv("SHIM_SPLIT", "chars")
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="x")], tools=tools)
    text, calls = _reply_from_result({"result": (tmp_path / "call.json.reply").read_text()}, tools)
    done = events[-1].message
    assert done.content == (text or None) and [c.name for c in done.tool_calls] == [c.name for c in calls]
    assert [c.arguments for c in done.tool_calls] == [c.arguments for c in calls]
    assert _streamed(events) == text
    await p.aclose()


@pytest.mark.parametrize(
    ("reply", "deltas", "shown"),
    [
        ("\n  hi there \n\n", ["\n ", " hi", " ", "there", " \n", "\n"], "hi there"),  # stripped like the final
        ("use <tool_ here", ["use <tool_", " here"], "use <tool_ here"),  # a held tail that turned out to be text
        ("ends <tool_c", ["ends <tool_c"], "ends <tool_c"),  # ...also at the very end
        ("before <tool_calls>[]</tool_calls> after", None, "before  after"),  # an empty block keeps the text
    ],
)
async def test_streamed_text_matches_the_final_content_at_the_edges(
    shim: Path, monkeypatch: pytest.MonkeyPatch, reply: str, deltas: list[str] | None, shown: str
) -> None:
    monkeypatch.setenv("SHIM_MODE", "probe")
    monkeypatch.setenv("SHIM_REPLIES", json.dumps({"probe": reply}))
    if deltas:
        monkeypatch.setenv("SHIM_DELTAS", json.dumps(deltas))
    else:
        monkeypatch.setenv("SHIM_SPLIT", "chars")
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="x")])
    assert _streamed(events) == shown == events[-1].message.content
    await p.aclose()


async def test_a_reply_line_longer_than_64k_is_read_whole(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_MODE", "huge")
    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim))
    events = await _collect(p, [Message(role="user", content="x")], tools=[])
    assert events[-1].message.content == "x" * 300_000 == _streamed(events)
    await p.aclose()


# ── --effort ──


@pytest.mark.parametrize(
    ("turn", "default", "sent"),
    [(None, None, None), ("xhigh", None, "xhigh"), (None, "low", "low"), ("high", "low", "high")],
)
async def test_effort_is_passed_from_effort_or_the_provider_default(
    shim: Path, tmp_path: Path, turn: str | None, default: str | None, sent: str | None
) -> None:
    from k3code.providers import effort

    p = ClaudeCliProvider(name="cc", persistent=False, command=str(shim), effort=default)
    token = effort.REASONING_EFFORT.set(turn)
    try:
        await _collect(p, [Message(role="user", content="hi")])
    finally:
        effort.REASONING_EFFORT.reset(token)
    argv = _call(tmp_path)["argv"]
    assert (argv[argv.index("--effort") + 1] if "--effort" in argv else None) == sent  # /effort wins
    await p.aclose()


def test_provider_entry_effort_is_validated_and_passed_through() -> None:
    (provider,) = make_providers([ProviderEntry(name="cc", kind="claude-cli", effort="medium")])
    assert provider.effort == "medium"
    assert ProviderEntry(name="cc", kind="claude-cli").effort is None
    with pytest.raises(ValidationError, match="effort must be one of"):
        ProviderEntry(name="cc", kind="claude-cli", effort="extreme")


# ── persistent sessions ──

PSHIM = r"""#!{python}
import json, os, sys, time
log = os.environ["SHIM_LOG"]
ctl = os.environ["SHIM_CTL"]

def append(suffix, obj):
    with open(log + suffix, "a") as fh:
        fh.write(json.dumps(obj) + "\n")

append(".spawns", {{"pid": os.getpid(), "argv": sys.argv[1:],
                    "auto_compact_off": os.environ.get("DISABLE_AUTO_COMPACT"),
                    "system": open(sys.argv[sys.argv.index("--system-prompt-file") + 1]).read()}})

def emit(obj):
    print(json.dumps(obj), flush=True)

def answer(text, n):
    append(".msgs", {{"pid": os.getpid(), "text": text}})
    c = json.load(open(ctl)) if os.path.exists(ctl) else {{}}
    mode, reply = c.get("mode", "text"), c.get("reply", "ok")
    if mode == "crash":
        sys.exit(3)
    if mode == "sleep":
        time.sleep(30)
    for piece in c.get("deltas") or [reply]:
        emit({{"type": "stream_event", "event": {{"type": "content_block_delta", "index": 0,
                                                 "delta": {{"type": "text_delta", "text": piece}}}}}})
    if c.get("gate"):
        for _ in range(500):
            if os.path.exists(c["gate"]):
                break
            time.sleep(0.02)
    if mode == "error":
        emit({{"type": "result", "subtype": "error", "is_error": True, "result": "boom", "api_error_status": 503}})
        return
    usage = {{"input_tokens": 2, "cache_read_input_tokens": 100 * n, "cache_creation_input_tokens": 10,
              "output_tokens": 5}}
    emit({{"type": "result", "subtype": "success", "is_error": False, "result": reply, "usage": usage,
           "total_cost_usd": 0.0125 * n}})  # the CLI reports a running total per process

if "--input-format" in sys.argv:
    for n, line in enumerate(sys.stdin, 1):
        answer(json.loads(line)["message"]["content"][0]["text"], n)
else:
    answer(sys.stdin.read(), 1)
"""


@pytest.fixture
def pshim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "claude-pshim"
    path.write_text(PSHIM.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("SHIM_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("SHIM_CTL", str(tmp_path / "ctl.json"))
    return path


def _ctl(tmp_path: Path, **kw) -> None:
    (tmp_path / "ctl.json").write_text(json.dumps(kw))


def _lines(tmp_path: Path, suffix: str) -> list[dict]:
    path = tmp_path / f"log{suffix}"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _close_and_check(p: ClaudeCliProvider, tmp_path: Path) -> None:
    await p.aclose()
    assert not p._pool
    assert not [s["pid"] for s in _lines(tmp_path, ".spawns") if _alive(s["pid"])]  # no fake process survives


def _bash(cmd: str) -> str:
    return f'Running it.\n<tool_calls>[{{"name": "bash", "arguments": {{"command": "{cmd}"}}}}]</tool_calls>'


def _round_trip(messages: list[Message]) -> list[Message]:
    """Through the gateway's session store format and back, as a resumed or rebuilt turn sees it."""
    from k3code.gateway.server import _serialize_messages

    stored = json.loads(json.dumps(_serialize_messages(messages)))
    return [
        Message(
            role=m["role"],
            content=m.get("content"),
            tool_call_id=m.get("tool_call_id"),
            name=m.get("name"),
            tool_calls=[
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"], raw_arguments=tc.get("raw_arguments"))
                for tc in m.get("tool_calls") or []
            ],
        )
        for m in stored
    ]


async def _step(p: ClaudeCliProvider, messages: list[Message], tmp_path: Path, reply: str, **kw) -> Message:
    _ctl(tmp_path, reply=reply)
    events = await _collect(p, messages, **kw)
    return events[-1].message


async def test_one_process_per_conversation_and_only_the_delta_is_sent(pshim: Path, tmp_path: Path) -> None:
    p = ClaudeCliProvider(name="cc", command=str(pshim))
    msgs = [Message(role="system", content="SYS"), Message(role="user", content="list the files")]
    a1 = await _step(p, msgs, tmp_path, _bash("ls"))
    msgs += [a1, Message(role="tool", content="a.py", tool_call_id=a1.tool_calls[0].id, name="bash")]
    a2 = await _step(p, msgs, tmp_path, _bash("cat a.py"))
    msgs += [a2, Message(role="tool", content="print(1)", tool_call_id=a2.tool_calls[0].id, name="bash")]
    msgs = _round_trip(msgs)  # the stored and rebuilt history still matches what the process covered
    _ctl(tmp_path, reply="It prints 1.")
    events = await _collect(p, msgs)
    assert events[-1].message.content == "It prints 1."
    spawns, sent = _lines(tmp_path, ".spawns"), _lines(tmp_path, ".msgs")
    assert len(spawns) == 1 and len(sent) == 3 and {m["pid"] for m in sent} == {spawns[0]["pid"]}
    assert "--input-format" in spawns[0]["argv"] and spawns[0]["system"] == "SYS"
    assert spawns[0]["auto_compact_off"] == "1"  # k3code compacts its own list; the mirror must not
    assert "unrelated scratch sandbox" in sent[0]["text"] and "list the files" in sent[0]["text"]
    for text, result, cmd in [(sent[1]["text"], "a.py", "ls"), (sent[2]["text"], "print(1)", "cat a.py")]:
        assert text.startswith(f'[tool result 1/1: bash {{"command": "{cmd}"}}]\n{result}')
        assert "list the files" not in text and "unrelated scratch sandbox" not in text and "<tool_calls>" in text
    assert events[-1].usage.cost_usd == pytest.approx(0.0125)  # this call's part of the running total
    await _close_and_check(p, tmp_path)


async def test_a_changed_prefix_or_key_starts_a_fresh_process(pshim: Path, tmp_path: Path) -> None:
    p = ClaudeCliProvider(name="cc", command=str(pshim), max_sessions=8)
    msgs = [Message(role="user", content="list the files")]
    a1 = await _step(p, msgs, tmp_path, _bash("ls"))
    result = Message(role="tool", content="a.py", tool_call_id=a1.tool_calls[0].id, name="bash")
    edited = [Message(role="user", content="list the FILES"), a1, result]  # e.g. compaction rewrote an old message
    await _step(p, edited, tmp_path, "done")
    sent = _lines(tmp_path, ".msgs")
    assert len(_lines(tmp_path, ".spawns")) == 2 and "list the FILES" in sent[-1]["text"]
    assert "(called tool bash id=" in sent[-1]["text"]  # the full render, not a delta
    base = [*msgs, a1, result]
    await _step(p, base, tmp_path, "done", model="m2")  # another model
    await _step(p, [Message(role="system", content="OTHER"), *base], tmp_path, "done")  # another system text
    await _step(p, base, tmp_path, "done", tools=TOOLS[:1])  # another tool list
    spawns = _lines(tmp_path, ".spawns")
    assert len(spawns) == 5 and len({s["pid"] for s in spawns}) == 5
    await _step(p, base, tmp_path, "done")  # the original key and prefix still continue the first process
    assert len(_lines(tmp_path, ".spawns")) == 5 and _lines(tmp_path, ".msgs")[-1]["pid"] == spawns[0]["pid"]
    await _close_and_check(p, tmp_path)


@pytest.mark.parametrize("mode", ["error", "crash", "sleep"])
async def test_a_failed_step_kills_the_process_and_the_retry_starts_fresh(
    pshim: Path, tmp_path: Path, mode: str
) -> None:
    p = ClaudeCliProvider(name="cc", command=str(pshim), timeout=1.0)
    msgs = [Message(role="user", content="list the files")]
    a1 = await _step(p, msgs, tmp_path, _bash("ls"))
    msgs += [a1, Message(role="tool", content="a.py", tool_call_id=a1.tool_calls[0].id, name="bash")]
    first = _lines(tmp_path, ".spawns")[0]["pid"]
    _ctl(tmp_path, mode=mode)
    with pytest.raises(ProviderError) as ei:
        await _collect(p, msgs)
    assert ei.value.status_code == {"error": 503, "crash": 502, "sleep": 504}[mode]
    assert not _alive(first) and not p._pool
    await _step(p, msgs, tmp_path, "done")
    spawns = _lines(tmp_path, ".spawns")
    assert len(spawns) == 2 and "unrelated scratch sandbox" in _lines(tmp_path, ".msgs")[-1]["text"]
    await _close_and_check(p, tmp_path)


async def test_a_rejected_reply_or_cancel_kills_the_process(pshim: Path, tmp_path: Path) -> None:
    import asyncio

    p = ClaudeCliProvider(name="cc", command=str(pshim))
    # cut off (opened, never closed) is still rejected; invalid JSON inside a closed block became an invalid_tool_call
    _ctl(tmp_path, reply='<tool_calls>[{"name": "bash"')
    with pytest.raises(ProviderError):
        await _collect(p, [Message(role="user", content="x")])
    assert not p._pool and not _alive(_lines(tmp_path, ".spawns")[0]["pid"])
    _ctl(tmp_path, mode="sleep")
    task = asyncio.create_task(_collect(p, [Message(role="user", content="x")]))
    for _ in range(200):
        if len(_lines(tmp_path, ".msgs")) == 2:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not p._pool and not _alive(_lines(tmp_path, ".spawns")[1]["pid"])
    await _close_and_check(p, tmp_path)


async def test_idle_processes_are_reaped(pshim: Path, tmp_path: Path) -> None:
    p = ClaudeCliProvider(name="cc", command=str(pshim), idle_seconds=0)
    msgs = [Message(role="user", content="hi")]
    a1 = await _step(p, msgs, tmp_path, "hello")
    first = _lines(tmp_path, ".spawns")[0]["pid"]
    await _step(p, [*msgs, a1, Message(role="user", content="again")], tmp_path, "ok")
    assert len(_lines(tmp_path, ".spawns")) == 2 and not _alive(first)
    await _close_and_check(p, tmp_path)


async def test_pool_cap_evicts_the_least_recently_used(pshim: Path, tmp_path: Path) -> None:
    p = ClaudeCliProvider(name="cc", command=str(pshim), max_sessions=2)
    convs = {k: [Message(role="user", content=k)] for k in "abc"}
    replies = {k: await _step(p, convs[k], tmp_path, f"re {k}") for k in "ab"}
    pids = [s["pid"] for s in _lines(tmp_path, ".spawns")]
    await _step(p, [*convs["a"], replies["a"], Message(role="user", content="more")], tmp_path, "ok")  # a is fresh
    await _step(p, convs["c"], tmp_path, "re c")  # the pool is full: b, the least recently used, goes
    assert len(_lines(tmp_path, ".spawns")) == 3 and not _alive(pids[1]) and _alive(pids[0]) and len(p._pool) == 2
    await _close_and_check(p, tmp_path)


async def test_streaming_works_and_a_full_pool_of_busy_processes_falls_back_to_one_shot(
    pshim: Path, tmp_path: Path
) -> None:
    import asyncio

    p = ClaudeCliProvider(name="cc", command=str(pshim), max_sessions=1)
    gate = tmp_path / "gate"
    _ctl(tmp_path, reply="hello", deltas=["hel", "lo"], gate=str(gate))
    first = p.stream([Message(role="user", content="a")], [], "m1")
    head = await anext(first)
    assert head.type == "text_delta" and head.text == "hel" and not gate.exists()  # streamed before the result
    other = asyncio.create_task(_collect(p, [Message(role="user", content="b")], tools=[]))
    for _ in range(200):
        if len(_lines(tmp_path, ".msgs")) == 2:
            break
        await asyncio.sleep(0.02)
    gate.touch()
    rest = [e async for e in first]
    assert _streamed([head, *rest]) == "hello" == rest[-1].message.content == (await other)[-1].message.content
    spawns = _lines(tmp_path, ".spawns")
    assert len(spawns) == 2 and "--input-format" not in spawns[1]["argv"]  # the busy process was not shared
    assert len(p._pool) == 1
    await _close_and_check(p, tmp_path)


def test_provider_entry_passes_the_session_settings_through() -> None:
    (provider,) = make_providers([ProviderEntry(name="cc", kind="claude-cli")])
    assert (provider.persistent, provider.max_sessions, provider.idle_seconds) == (True, 4, 600.0)
    entry = ProviderEntry(name="cc", kind="claude-cli", persistent=False, max_sessions=2, idle_seconds=30)
    (provider,) = make_providers([entry])
    assert (provider.persistent, provider.max_sessions, provider.idle_seconds) == (False, 2, 30.0)
