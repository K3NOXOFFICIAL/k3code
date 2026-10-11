"""Prompt caching: cache_control breakpoints on the Anthropic payload, opt-in for OpenAI-compatible relays to Claude,
and cache read/write tokens recorded in usage.db."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from pydantic import ValidationError

from k3code.config import ProviderEntry
from k3code.providers import make_providers
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message, ToolCall, ToolSpec
from k3code.usage import UsageDB, format_stats

TOOLS = [
    ToolSpec(name="read", description="read a file", parameters={"type": "object", "properties": {}}),
    ToolSpec(name="grep", description="search", parameters={"type": "object", "properties": {}}),
]


def conversation() -> list[Message]:
    call = ToolCall(id="c1", name="read", arguments={"path": "a.py"})
    return [
        Message(role="system", content="SYSTEM PROMPT"),
        Message(role="user", content="look at a.py"),
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", content="1\tprint('hi')", tool_call_id="c1", name="read"),
    ]


def breakpoints(payload: dict) -> int:
    return json.dumps(payload).count('"cache_control"')


def test_the_anthropic_payload_marks_system_last_tool_and_newest_message():
    p = AnthropicProvider(name="a", api_key="k")
    payload = p._payload(conversation(), TOOLS, "claude-test", max_tokens=100, temperature=None)
    eph = {"type": "ephemeral"}
    assert payload["system"] == [{"type": "text", "text": "SYSTEM PROMPT", "cache_control": eph}]
    assert payload["tools"][-1]["cache_control"] == eph and "cache_control" not in payload["tools"][0]
    last = payload["messages"][-1]["content"][-1]
    assert last["type"] == "tool_result" and last["cache_control"] == eph
    assert all("cache_control" not in b for m in payload["messages"][:-1] for b in m["content"])
    assert breakpoints(payload) == 3 <= 4


def test_the_moving_breakpoint_follows_a_plain_user_message_and_never_lands_on_empty_text():
    p = AnthropicProvider(name="a", api_key="k")
    msgs = [Message(role="system", content="S"), Message(role="user", content="hello")]
    payload = p._payload(msgs, TOOLS, "claude-test", max_tokens=100, temperature=None)
    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]
    assert breakpoints(payload) == 3
    # a call without tools (title, classifier, judge, summary) is asked once: the newest message is never read back, so
    # it does not carry the cache-write surcharge; the system prompt still does
    once = p._payload(msgs, [], "claude-test", max_tokens=100, temperature=None)
    assert once["messages"][-1]["content"] == "hello" and "tools" not in once and breakpoints(once) == 1
    empty = p._payload([Message(role="user", content="")], [], "claude-test", max_tokens=100, temperature=None)
    assert breakpoints(empty) == 0 and "system" not in empty
    silent = [*conversation()[:3], Message(role="tool", content="", tool_call_id="c1", name="read")]
    payload = p._payload(silent, [], "claude-test", max_tokens=100, temperature=None)
    assert breakpoints(payload) == 1  # the system prompt only: never on an empty tool result


def test_prompt_cache_off_sends_no_breakpoints():
    p = AnthropicProvider(name="a", api_key="k", prompt_cache="off")
    payload = p._payload(conversation(), TOOLS, "claude-test", max_tokens=100, temperature=None)
    assert breakpoints(payload) == 0 and payload["system"] == "SYSTEM PROMPT"


def test_openai_compatible_marks_messages_only_when_not_off_and_the_model_is_claude():
    auto = OpenAICompatProvider(name="o", base_url="https://relay.test/v1", api_key="k")
    assert breakpoints(auto._payload(conversation(), TOOLS, "claude-test", max_tokens=100, temperature=None)) == 2
    assert breakpoints(auto._payload(conversation(), TOOLS, "gpt-4o", max_tokens=100, temperature=None)) == 0
    off = OpenAICompatProvider(name="o", base_url="https://relay.test/v1", api_key="k", prompt_cache="off")
    assert breakpoints(off._payload(conversation(), TOOLS, "claude-test", max_tokens=100, temperature=None)) == 0
    on = OpenAICompatProvider(name="o", base_url="https://relay.test/v1", api_key="k", prompt_cache="on")
    assert breakpoints(on._payload(conversation(), TOOLS, "gpt-4o", max_tokens=100, temperature=None)) == 0
    payload = on._payload(conversation(), TOOLS, "anthropic/claude-test", max_tokens=100, temperature=None)
    msgs = payload["messages"]
    assert msgs[0]["content"] == [{"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}]
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(payload["tools"])  # inside messages only
    assert breakpoints(payload) == 2


def test_prompt_cache_is_validated_and_reaches_the_providers():
    with pytest.raises(ValidationError):
        ProviderEntry(name="x", kind="anthropic", base_url="u", api_key_env="E", prompt_cache="maybe")
    entries = [
        ProviderEntry(name="a", kind="anthropic", base_url="https://a.test", api_key_env="E", prompt_cache="off"),
        ProviderEntry(name="o", kind="openai", base_url="https://o.test", api_key_env="E", prompt_cache="on"),
        ProviderEntry(name="d", kind="anthropic", base_url="https://a.test", api_key_env="E"),
    ]
    a, o, d = make_providers(entries)
    assert (a.prompt_cache, o.prompt_cache, d.prompt_cache) == (False, True, True)


def sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


async def test_cache_read_and_write_tokens_are_parsed_from_the_anthropic_usage_block():
    body = sse(
        {
            "type": "message_start",
            "message": {
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_delta", "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body)))
    p = AnthropicProvider(name="a", api_key="k", client=client)
    events = [e async for e in p.stream([Message(role="user", content="hi")], [], "claude-test")]
    usage = events[-1].usage
    assert (usage.prompt_tokens, usage.cache_read_tokens, usage.cache_creation_tokens) == (960, 900, 50)
    await client.aclose()


def test_openai_usage_reports_cached_tokens():
    from k3code.providers.openai_compat import _parse_usage

    u = _parse_usage({"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 80}})
    assert (u.cache_read_tokens, u.cache_creation_tokens) == (80, 0)


def test_usage_db_records_cache_tokens_and_migrates_an_old_database(tmp_path):
    path = tmp_path / "usage.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE events (ts REAL NOT NULL, day TEXT NOT NULL, session TEXT NOT NULL DEFAULT '', kind TEXT NOT"
        " NULL, provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', tokens_in INTEGER NOT NULL"
        " DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost_usd REAL, seconds REAL NOT NULL DEFAULT 0,"
        " detail TEXT NOT NULL DEFAULT '')"
    )
    old.execute("INSERT INTO events (ts, day, session, kind, tokens_in) VALUES (1, '1970-01-01', 's', 'call', 7)")
    old.commit()
    old.close()
    db = UsageDB(path)
    db.record("call", session="s", tokens_in=1000, tokens_out=10, cache_read=800, cache_write=150)
    (row,) = db.aggregate("session")
    assert (row["tokens_in"], row["cache_read"], row["cache_write"]) == (1007, 800, 150)
    assert "prompt cache read/write 800/150 tok" in format_stats([row], "session")
    db.close()


def test_the_usage_wire_payload_carries_the_cache_tokens():
    from k3code.gateway.server import _usage_payload
    from k3code.providers.types import Usage

    payload = _usage_payload(
        Usage(prompt_tokens=1000, completion_tokens=10, cache_read_tokens=800, cache_creation_tokens=150)
    )
    assert payload == {
        "prompt_tokens": 1000,
        "completion_tokens": 10,
        "total_tokens": 1010,
        "cache_read_tokens": 800,
        "cache_write_tokens": 150,
    }
