"""What keeps a conversation's request prefix the same from one call to the next, and for how long it stays cached."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from k3code.config import ProviderEntry
from k3code.gateway.server import LiveSession
from k3code.providers import make_providers
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message, messages_to_anthropic
from test_token_prompt_cache import TOOLS, conversation


def test_a_one_hour_cache_entry_is_marked_on_every_breakpoint():
    p = AnthropicProvider(name="a", api_key="k", cache_ttl="1h")
    payload = p._payload(conversation(), TOOLS, "claude-test", max_tokens=100, temperature=None)
    marks = [m for m in json.dumps(payload).split('"cache_control": ')[1:]]
    assert len(marks) == 3 and all(m.startswith('{"type": "ephemeral", "ttl": "1h"}') for m in marks)
    default = AnthropicProvider(name="a", api_key="k")
    assert '"ttl"' not in json.dumps(
        default._payload(conversation(), TOOLS, "claude-test", max_tokens=1, temperature=None)
    )


def test_a_relay_to_claude_gets_the_same_ttl():
    on = OpenAICompatProvider(
        name="o", base_url="https://relay.test/v1", api_key="k", prompt_cache="on", cache_ttl="1h"
    )
    payload = on._payload(conversation(), TOOLS, "anthropic/claude-test", max_tokens=100, temperature=None)
    assert json.dumps(payload).count('"ttl": "1h"') == 2


def test_cache_ttl_is_validated_and_reaches_the_providers():
    with pytest.raises(ValidationError):
        ProviderEntry(name="x", kind="anthropic", base_url="u", api_key_env="E", cache_ttl="2h")
    entry = ProviderEntry(name="a", kind="anthropic", base_url="https://a.test", api_key_env="E", cache_ttl="1h")
    (provider,) = make_providers([entry])
    assert provider.cache_ttl == "1h"


def stored(*messages: dict) -> SimpleNamespace:
    return SimpleNamespace(stored=SimpleNamespace(messages=list(messages)))


def test_a_note_sent_in_the_middle_of_a_turn_is_sent_again_in_the_next_one():
    """The loop guard's note and a [learned] lesson join the conversation after a tool result. The provider cached the
    request with them in it; a history without them changes the request from that message on."""
    note = "[learned] bash needs a timeout here"
    call = {"id": "1", "name": "bash", "arguments": {"command": "make"}}
    turn_one = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "name": "bash", "tool_call_id": "1", "content": "failed"},
        {"role": "system", "content": note},
        {"role": "assistant", "content": "retrying"},
    ]
    history = LiveSession.history.fget(stored(*turn_one))
    assert [m.role for m in history] == ["user", "assistant", "tool", "system", "assistant"]
    # the request the next turn sends starts with exactly what the first turn sent
    first = messages_to_anthropic([Message(role="system", content="SYSTEM PROMPT"), *history])[1]
    again = messages_to_anthropic(
        [Message(role="system", content="SYSTEM PROMPT"), *history, Message(role="user", content="and now?")]
    )[1]
    assert again[: len(first)] == first
    assert note in json.dumps(first)


def test_only_the_leading_system_entries_are_dropped_and_empty_ones_never_sent():
    history = LiveSession.history.fget(
        stored(
            {"role": "system", "content": "SYSTEM PROMPT"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": ""},
            {"role": "assistant", "content": "hello"},
        )
    )
    assert [(m.role, m.content) for m in history] == [("user", "hi"), ("assistant", "hello")]


async def test_a_new_turn_starts_with_the_same_elisions_the_last_one_ended_with(tmp_path):
    """Past half the window old tool results are elided. The next turn used to start with nothing elided and elide them
    all again at its first request, so that request differed from the last one of the turn before: a cache miss."""
    from k3code.agent.loop import AgentLoop
    from k3code.providers.types import ToolCall
    from k3code.reliability import Reliability
    from k3code.router import Router, build_chain
    from test_wire_clip import Recorder, text_reply, tool_reply

    files = []
    for i in range(12):
        f = tmp_path / f"f{i}.txt"
        f.write_text(f"file {i}\n" + ("q" * 59 + "\n") * 50)  # ~3 kB
        files.append(ToolCall(id=f"r{i}", name="read", arguments={"path": str(f)}))
    elided: set[str] = set()
    notes: dict[str, str] = {}

    def loop_for(provider, name: str) -> AgentLoop:
        loop = AgentLoop(
            Router(build_chain([provider], [["m"]]), max_retries=0),
            system_prompt="You are a test agent.",
            max_turns=40,
            permission_mode="yolo",
            cwd=tmp_path,
            session="s",
            reliability=Reliability.from_settings(None, session=name, home=tmp_path / "home"),
            context_window=10_000,
        )
        loop.share_elision(elided, notes)
        return loop

    first = Recorder([*(tool_reply(c) for c in files), text_reply("done")])
    one = loop_for(first, "one")
    async for _ in one.run("read them all"):
        pass
    assert elided  # the first turn crossed the threshold and elided its old results
    last_of_first = [m.content for m in first.requests[-1][0]]

    second = Recorder([text_reply("again")])
    two = loop_for(second, "two")
    history = [m for m in one.turn_messages if m.role != "system"]
    async for _ in two.run("and now?", history=history):
        pass
    start_of_second = [m.content for m in second.requests[0][0]]
    # everything the last request of the first turn sent is sent again, byte for byte, ahead of the new prompt
    assert start_of_second[: len(last_of_first)] == last_of_first


# ── Claude behind an OpenAI-compatible relay ──────────────────────────


def _relay(accepts_markers: bool, seen: list[bool], status_without: int = 200):
    """A relay on httpx's mock transport: 400 to a request with cache_control when it does not take the field."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        marked = b"cache_control" in request.content
        seen.append(marked)
        if marked and not accepts_markers:
            return httpx.Response(400, json={"error": {"message": "Extra inputs are not permitted: cache_control"}})
        if not marked and status_without != 200:
            return httpx.Response(status_without, json={"error": {"message": "maximum context length is 1000 tokens"}})
        body = (
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _ask(provider, model: str = "anthropic/claude-sonnet-5-5"):
    return [e async for e in provider.stream(conversation(), TOOLS, model)]


async def test_auto_sends_the_markers_to_a_relay_that_takes_them():
    seen: list[bool] = []
    p = OpenAICompatProvider(name="o", base_url="https://relay.test/v1", api_key="k", client=_relay(True, seen))
    await _ask(p)
    await _ask(p)
    assert seen == [True, True] and p.prompt_cache


async def test_auto_drops_the_markers_for_good_when_the_relay_rejects_them():
    seen: list[bool] = []
    p = OpenAICompatProvider(name="o", base_url="https://relay.test/v1", api_key="k", client=_relay(False, seen))
    events = await _ask(p)
    assert events[-1].type == "done" and seen == [True, False]  # rejected, then sent again without
    await _ask(p)
    assert seen == [True, False, False] and not p.prompt_cache  # the next request does not try again


async def test_a_400_that_has_nothing_to_do_with_the_markers_is_raised_and_the_markers_stay():
    from k3code.providers.base import ProviderError

    seen: list[bool] = []
    p = OpenAICompatProvider(
        name="o", base_url="https://relay.test/v1", api_key="k", client=_relay(True, seen, status_without=400)
    )
    ok = await _ask(p)  # markers accepted: a normal answer
    assert ok[-1].type == "done"
    never = OpenAICompatProvider(
        name="o", base_url="https://relay.test/v1", api_key="k", client=_relay(False, [], status_without=400)
    )
    with pytest.raises(ProviderError) as err:
        await _ask(never)
    assert err.value.status_code == 400 and never.prompt_cache  # it failed both ways: not the markers' fault


async def test_on_never_retries_and_a_gpt_model_never_carries_markers():
    from k3code.providers.base import ProviderError

    seen: list[bool] = []
    forced = OpenAICompatProvider(
        name="o", base_url="https://relay.test/v1", api_key="k", prompt_cache="on", client=_relay(False, seen)
    )
    with pytest.raises(ProviderError):
        await _ask(forced)
    assert seen == [True]
    seen.clear()
    auto = OpenAICompatProvider(name="o", base_url="https://relay.test/v1", api_key="k", client=_relay(False, seen))
    await _ask(auto, "gpt-4o")
    assert seen == [False]
