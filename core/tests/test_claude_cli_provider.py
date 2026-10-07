"""The claude-cli provider: stateless `claude -p` per turn, built-in tools off, plain text + a final tool block.

A shim script stands in for the `claude` binary: it records its argv, stdin, cwd and a few env
variables, then prints a canned CLI JSON result chosen by the SHIM_MODE variable.
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
from k3code.providers.claude_cli import render_prompt
from k3code.providers.types import Message, ToolCall, ToolSpec

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
            "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "OMNIROUTE_API_KEY", "HOME")}},
    }}, fh)
mode = os.environ.get("SHIM_MODE", "text")
usage = {{"input_tokens": 3, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50, "output_tokens": 7}}
CALLS = '[{{"name": "read_file", "arguments": {{"path": "a.py"}}}}, {{"name": "nope", "arguments": {{}}}}]'
replies = {{
    "text": "hello",
    "long": "line one\n```\n[ todo ]\n```\nRisks:\n- a\n- b\n- c",
    "tool": "Let me read it.\n<tool_calls>" + CALLS + "</tool_calls>",
    "fenced": "<tool_calls>\n```json\n" + CALLS + "\n```\n</tool_calls>",
    "twice": "<tool_calls>[]</tool_calls> oops <tool_calls>" + CALLS + "</tool_calls>",
    "bad": "<tool_calls>[{{not json</tool_calls>",
    "cut": "start <tool_calls>[{{\"name\": \"read_file\"",
}}
if mode in replies:
    print(json.dumps({{"is_error": False, "usage": usage, "result": replies[mode]}}))
elif mode == "login":
    print(json.dumps({{"is_error": True, "result": "Not logged in · Please run /login"}})); sys.exit(1)
elif mode == "limit":
    print(json.dumps({{"is_error": True, "result": "5-hour limit reached. Resets at 21:00"}})); sys.exit(1)
elif mode == "status":
    print(json.dumps({{"is_error": True, "result": "boom", "api_error_status": 503}})); sys.exit(1)
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
    p = ClaudeCliProvider(name="cc", command=str(shim))
    events = await _collect(p, [Message(role="user", content="hi")])
    assert [e.type for e in events] == ["text_delta", "done"]
    assert events[0].text == "hello"
    done = events[-1]
    assert done.message.content == "hello" and not done.message.tool_calls
    assert done.usage.prompt_tokens == 153 and done.usage.completion_tokens == 7  # input + cache read + cache write
    await p.aclose()


async def test_tool_calls_become_k3code_tool_calls(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_MODE", "tool")
    p = ClaudeCliProvider(name="cc", command=str(shim))
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
    p = ClaudeCliProvider(name="cc", command=str(shim))
    events = await _collect(p, [Message(role="user", content="x")])
    assert [e.tool_call.name for e in events if e.type == "tool_call"] == ["read_file", "nope"]
    await p.aclose()


async def test_long_reply_is_returned_whole_without_tools(shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_MODE", "long")  # /preview-style answer: nothing may be lost or summarised
    p = ClaudeCliProvider(name="cc", command=str(shim))
    events = await _collect(p, [Message(role="user", content="x")], tools=[])
    assert events[0].text.endswith("- c") and "[ todo ]" in events[0].text
    await p.aclose()


@pytest.mark.parametrize("mode", ["bad", "cut"])
async def test_malformed_tool_block_is_a_provider_error(shim: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("SHIM_MODE", mode)
    p = ClaudeCliProvider(name="cc", command=str(shim))
    with pytest.raises(ProviderError) as ei:
        await _collect(p, [Message(role="user", content="x")])
    assert ei.value.status_code == 502 and "tool_calls" in ei.value.message
    await p.aclose()


async def test_isolation_flags_env_and_cwd(shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://relay.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "secret-key")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    p = ClaudeCliProvider(name="cc", command=str(shim))
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
    p = ClaudeCliProvider(name="cc", command=str(shim))
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
    p = ClaudeCliProvider(name="cc", command=str(shim))
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
    p = ClaudeCliProvider(name="cc", command=str(shim), timeout=0.5)
    with pytest.raises(ProviderError) as ei:
        await _collect(p, [Message(role="user", content="x")])
    assert ei.value.status_code == 504
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
