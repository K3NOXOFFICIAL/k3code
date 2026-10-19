"""Prompt-cache TTL policy (``prompt_cache_ttl``) and no cache breakpoints on tool-less side calls."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from k3code.config import ProviderEntry, Settings
from k3code.providers import make_providers
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.base import ProviderError
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message, ToolCall, ToolSpec

FIRST_PARTY = "https://api.anthropic.com"
RELAY = "https://relay.test"
EPH = {"type": "ephemeral"}
EPH_1H = {"type": "ephemeral", "ttl": "1h"}
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


def payload_for(base_url: str, ttl: str = "auto", tools: list[ToolSpec] | None = TOOLS) -> dict:
    p = AnthropicProvider(name="a", base_url=base_url, api_key="k", prompt_cache_ttl=ttl)
    return p._payload(conversation(), tools or [], "claude-test", max_tokens=100, temperature=None)


def marks_in_request_order(payload: dict) -> list[dict]:
    """Every cache_control in the order the API reads the prompt: tools, then system, then messages."""
    found: list[dict] = []
    for tool in payload.get("tools") or []:
        if "cache_control" in tool:
            found.append(tool["cache_control"])
    system = payload.get("system")
    for block in system if isinstance(system, list) else []:
        if "cache_control" in block:
            found.append(block["cache_control"])
    for message in payload["messages"]:
        for block in message["content"] if isinstance(message["content"], list) else []:
            if "cache_control" in block:
                found.append(block["cache_control"])
    return found


def test_first_party_auto_puts_1h_on_tools_and_system_and_keeps_the_tail_at_5m():
    payload = payload_for(FIRST_PARTY)
    assert payload["tools"][-1]["cache_control"] == EPH_1H
    assert payload["system"] == [{"type": "text", "text": "SYSTEM PROMPT", "cache_control": EPH_1H}]
    assert payload["messages"][-1]["content"][-1]["cache_control"] == EPH  # newest message
    assert payload["messages"][0]["content"][-1]["cache_control"] == EPH  # before the newest assistant message
    assert json.dumps(payload).count('"ttl"') == 2


def test_relay_auto_sends_no_ttl_and_the_same_four_breakpoints_as_before():
    payload = payload_for(RELAY)
    assert '"ttl"' not in json.dumps(payload)
    assert marks_in_request_order(payload) == [EPH, EPH, EPH, EPH]


def test_1h_is_forced_on_a_relay_and_5m_never_sends_the_field():
    forced = payload_for(RELAY, "1h")
    assert marks_in_request_order(forced) == [EPH_1H, EPH_1H, EPH, EPH]
    for base_url in (FIRST_PARTY, RELAY):
        assert '"ttl"' not in json.dumps(payload_for(base_url, "5m"))
        assert len(marks_in_request_order(payload_for(base_url, "5m"))) == 4


def test_the_first_party_host_is_taken_from_the_base_url_not_the_model_name():
    assert '"ttl"' in json.dumps(payload_for("https://api.anthropic.com/"))
    assert '"ttl"' not in json.dumps(payload_for("https://api.anthropic.com.relay.test"))
    assert '"ttl"' not in json.dumps(payload_for("https://relay.test/api.anthropic.com"))


@pytest.mark.parametrize("base_url,ttl", [(FIRST_PARTY, "auto"), (RELAY, "1h"), (RELAY, "auto"), (FIRST_PARTY, "5m")])
def test_longer_ttl_breakpoints_never_follow_shorter_ones(base_url, ttl):
    seconds = [3600 if m.get("ttl") == "1h" else 300 for m in marks_in_request_order(payload_for(base_url, ttl))]
    assert seconds and seconds == sorted(seconds, reverse=True)


def test_a_system_prompt_without_tools_or_tools_without_system_keep_the_order():
    p = AnthropicProvider(name="a", api_key="k")
    no_system = p._payload(conversation()[1:], TOOLS, "claude-test", max_tokens=100, temperature=None)
    assert marks_in_request_order(no_system) == [EPH_1H, EPH, EPH]


def test_prompt_cache_off_wins_over_a_forced_1h():
    p = AnthropicProvider(name="a", api_key="k", prompt_cache="off", prompt_cache_ttl="1h")
    payload = p._payload(conversation(), TOOLS, "claude-test", max_tokens=100, temperature=None)
    assert "cache_control" not in json.dumps(payload)


@pytest.mark.parametrize("base_url,ttl", [(FIRST_PARTY, "auto"), (RELAY, "1h"), (RELAY, "auto")])
def test_a_tool_less_anthropic_request_carries_no_cache_control(base_url, ttl):
    assert "cache_control" not in json.dumps(payload_for(base_url, ttl, tools=[]))
    assert len(marks_in_request_order(payload_for(base_url, ttl))) == 4  # with tools it keeps its breakpoints


def test_a_tool_less_relay_request_carries_no_cache_control():
    for kw in ({}, {"prompt_cache": "on"}):
        p = OpenAICompatProvider(name="o", base_url="https://relay.test/v1", api_key="k", **kw)
        bare = p._payload(conversation(), [], "claude-test", max_tokens=100, temperature=None)
        assert "cache_control" not in json.dumps(bare)
        with_tools = p._payload(conversation(), TOOLS, "claude-test", max_tokens=100, temperature=None)
        assert json.dumps(with_tools).count('"cache_control"') == 2
        assert '"ttl"' not in json.dumps(with_tools)


def _sse_ok() -> bytes:
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


async def test_a_400_naming_the_ttl_drops_it_for_good_and_the_retry_works():
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        sent.append(body)
        if '"ttl"' in json.dumps(body):
            msg = "system.0.cache_control.ephemeral.ttl: Extra inputs are not permitted"
            return httpx.Response(
                400, json={"type": "error", "error": {"type": "invalid_request_error", "message": msg}}
            )
        return httpx.Response(200, content=_sse_ok())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = AnthropicProvider(name="a", base_url=FIRST_PARTY, api_key="k", client=client)
    events = [e async for e in p.stream(conversation(), TOOLS, "claude-test")]
    assert any(getattr(e, "text", None) == "ok" for e in events)
    assert len(sent) == 2 and '"ttl"' not in json.dumps(sent[1])
    assert marks_in_request_order(sent[1]) == [EPH, EPH, EPH, EPH]  # still cached, at 5m
    [e async for e in p.stream(conversation(), TOOLS, "claude-test")]
    assert len(sent) == 3 and '"ttl"' not in json.dumps(sent[2])  # no further rejected try
    await client.aclose()


async def test_a_400_without_a_ttl_in_the_request_is_raised_as_before():
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "bad cache_control"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = AnthropicProvider(name="a", base_url=RELAY, api_key="k", client=client)
    with pytest.raises(ProviderError):
        [e async for e in p.stream(conversation(), TOOLS, "claude-test")]
    assert len(sent) == 1
    await client.aclose()


@pytest.mark.parametrize("base_url,expected_tries", [(RELAY, 1), (FIRST_PARTY, 2)])
async def test_a_tool_schema_with_a_ttl_property_never_turns_a_400_into_a_resend_loop(base_url, expected_tries):
    schema = {"type": "object", "properties": {"ttl": {"type": "string"}}}
    tools = [*TOOLS, ToolSpec(name="cache_set", description="set", parameters=schema)]
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        if len(sent) < 3:  # a 200 on the third try lets a looping provider end instead of hanging the test
            return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "ttl too long"}})
        return httpx.Response(200, content=_sse_ok())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = AnthropicProvider(name="a", base_url=base_url, api_key="k", client=client)
    with pytest.raises(ProviderError):
        [e async for e in p.stream(conversation(), tools, "claude-test")]
    assert len(sent) == expected_tries
    await client.aclose()


def test_prompt_cache_ttl_is_validated_and_reaches_the_anthropic_provider():
    base = {"name": "a", "kind": "anthropic", "base_url": RELAY, "api_key_env": "K"}
    with pytest.raises(ValidationError):
        ProviderEntry(**base, prompt_cache_ttl="2h")
    assert ProviderEntry(**base).prompt_cache_ttl == "auto"
    [provider] = make_providers([ProviderEntry(**base, prompt_cache_ttl="1h")])
    assert isinstance(provider, AnthropicProvider) and provider.long_ttl


def test_doctor_shows_the_ttl_policy_per_provider():
    from k3code.doctor import OK, WARN, check_prompt_cache_ttl

    def entry(name: str, **kw) -> ProviderEntry:
        return ProviderEntry(**{"name": name, "kind": "anthropic", "base_url": RELAY, "api_key_env": "K", **kw})

    checks = check_prompt_cache_ttl(
        Settings(
            providers=[
                entry("direct", base_url=FIRST_PARTY),
                entry("relay"),
                entry("forced", prompt_cache_ttl="1h"),
                entry("off", prompt_cache="off"),
                entry("gw", kind="openai", prompt_cache_ttl="1h"),
            ]
        )
    )
    by_name = {c.name: c for c in checks}
    assert (
        by_name["prompt-cache-ttl:direct"].status == OK
        and "1h on tools and system" in by_name["prompt-cache-ttl:direct"].detail
    )
    assert "5m on tools and system" in by_name["prompt-cache-ttl:relay"].detail
    assert by_name["prompt-cache-ttl:forced"].data["stable"] == "1h"
    assert "prompt-cache-ttl:off" not in by_name
    assert by_name["prompt-cache-ttl:gw"].status == WARN
