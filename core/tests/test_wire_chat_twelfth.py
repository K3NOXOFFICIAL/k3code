"""Chat-completions wire: alternating roles after a compaction, odd delta shapes, parallel calls without ids, the
temperature of reasoning models, refusals and empty assistant messages."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from k3code.providers.base import ProviderError
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message, ToolCall, messages_to_openai


def _sse(*chunks: dict[str, Any]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def _delta(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _call(index: int, fn: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"tool_calls": [{"index": index, "function": fn, **extra}]}


def _provider(body: bytes, sent: list[dict[str, Any]] | None = None) -> OpenAICompatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider(name="o", base_url="http://relay.test/v1", api_key="k", client=client)


def _final(body: bytes, model: str = "gpt-x", **kw: Any) -> Message:
    async def go() -> Message:
        events = [ev async for ev in _provider(body).stream([Message(role="user", content="hi")], [], model, **kw)]
        return events[-1].message

    return asyncio.run(go())


def test_consecutive_user_messages_are_joined_on_the_chat_wire():
    # after a compaction: [system, kept history (user), summary (user), pinned note (user), the new question]
    msgs = [
        Message(role="system", content="S"),
        Message(role="user", content="old question"),
        Message(role="user", content="SUMMARY"),
        Message(role="user", content="pinned"),
        Message(role="user", content="new question"),
    ]
    wire = messages_to_openai(msgs)
    assert [m["role"] for m in wire] == ["system", "user"]
    assert wire[1]["content"] == "old question\n\nSUMMARY\n\npinned\n\nnew question"
    assert [m.content for m in msgs[1:]] == ["old question", "SUMMARY", "pinned", "new question"]  # history untouched


def test_user_messages_around_a_tool_exchange_stay_apart():
    call = ToolCall(id="c1", name="read", arguments={})
    msgs = [
        Message(role="user", content="a"),
        Message(role="assistant", content=None, tool_calls=[call]),
        Message(role="tool", content="r", tool_call_id="c1", name="read"),
        Message(role="user", content="b"),
    ]
    assert [m["role"] for m in messages_to_openai(msgs)] == ["user", "assistant", "tool", "user"]


def test_empty_assistant_message_is_skipped_and_the_users_around_it_join():
    msgs = [
        Message(role="user", content="a"),
        Message(role="assistant", content=None),
        Message(role="user", content="b"),
    ]
    wire = messages_to_openai(msgs)
    assert wire == [{"role": "user", "content": "a\n\nb"}]


def test_delta_content_as_a_list_of_parts_is_text():
    body = _sse(
        _delta({"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Hi"}]}),
        _delta({"content": [{"type": "text", "text": " there"}]}, "stop"),
    )
    assert _final(body).content == "Hi there"


def test_tool_call_arguments_as_an_object_are_json():
    body = _sse(_delta(_call(0, {"name": "read", "arguments": {"path": "/a"}}, id="c0"), "tool_calls"))
    call = _final(body).tool_calls[0]
    assert call.arguments == {"path": "/a"}


def test_an_unreadable_delta_is_a_non_retryable_error():
    body = _sse(_delta({"content": 42}, "stop"))
    with pytest.raises(ProviderError) as info:
        _final(body)
    assert info.value.status_code == 400
    assert "delta.content" in str(info.value)


def test_parallel_calls_numbered_zero_without_ids_stay_separate():
    body = _sse(
        _delta(_call(0, {"name": "read", "arguments": '{"path":"/a"}'})),
        _delta(_call(0, {"name": "grep", "arguments": '{"pattern":"x"}'}), "stop"),
    )
    calls = _final(body).tool_calls
    assert [(c.name, c.arguments) for c in calls] == [("read", {"path": "/a"}), ("grep", {"pattern": "x"})]


def test_parallel_calls_with_empty_ids_and_the_same_name_stay_separate():
    body = _sse(
        _delta(_call(0, {"name": "read", "arguments": '{"path":"/a"}'}, id="")),
        _delta(_call(0, {"name": "read", "arguments": '{"path":"/b"}'}, id=""), "stop"),
    )
    calls = _final(body).tool_calls
    assert [c.arguments for c in calls] == [{"path": "/a"}, {"path": "/b"}]
    assert calls[0].id != calls[1].id


def test_one_call_streamed_in_pieces_is_still_one_call():
    body = _sse(
        _delta(_call(0, {"name": "re", "arguments": ""}, id="c0")),
        _delta(_call(0, {"name": "ad", "arguments": '{"path"'})),
        _delta(_call(0, {"name": "read", "arguments": ':"/a"}'}), "tool_calls"),  # a server that repeats the name
    )
    calls = _final(body).tool_calls
    assert [(c.id, c.name, c.arguments) for c in calls] == [("c0", "read", {"path": "/a"})]


def test_a_refusal_is_streamed_as_text():
    body = _sse(_delta({"role": "assistant", "refusal": "I can't help with that."}), _delta({}, "stop"))
    assert _final(body).content == "I can't help with that."


@pytest.mark.parametrize(
    ("model", "keeps"),
    [
        ("o3", False),
        ("o1-mini", False),
        ("o4-mini", False),
        ("gpt-5", False),
        ("openai/gpt-5-mini", False),
        ("claude-sonnet-5-5", False),  # a Claude id relayed through the chat wire: the Anthropic rule
        ("gpt-4o", True),
        ("gpt-5-chat-latest", True),
        ("gemini-3-pro", True),
        ("llama-3.1-70b", True),
    ],
)
def test_temperature_is_dropped_for_models_that_refuse_it(model: str, keeps: bool):
    sent: list[dict[str, Any]] = []
    body = _sse(_delta({"content": "ok"}, "stop"))

    async def go() -> None:
        async for _ in _provider(body, sent).stream([Message(role="user", content="hi")], [], model, temperature=0.3):
            pass

    asyncio.run(go())
    assert ("temperature" in sent[0]) is keeps
