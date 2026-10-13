"""A sub-agent's system prompt is two messages (what its siblings share, then what is its own): the native Anthropic API
caches them as two blocks; other providers get one."""

from __future__ import annotations

import httpx

from k3code.agent.loop import AgentLoop
from k3code.config import Settings
from k3code.pricing import estimate_usd
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message, StreamEvent, ToolSpec, Usage, messages_to_openai
from k3code.router import Router, build_chain

TOOL = ToolSpec(name="read", description="d", parameters={"type": "object", "properties": {}})


def msgs(*system: str) -> list[Message]:
    return [*(Message(role="system", content=s) for s in system), Message(role="user", content="go")]


def anthropic(**kw) -> AnthropicProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    return AnthropicProvider(name="a", base_url="https://a.test", api_key="k", client=client, **kw)


def test_two_system_messages_become_two_cached_blocks():
    payload = anthropic()._payload(
        msgs("SHARED base", "[agent:worker] fix a.py"), [TOOL], "claude-sonnet-5-5", max_tokens=100, temperature=None
    )
    blocks = payload["system"]
    assert [b["text"] for b in blocks] == ["SHARED base", "[agent:worker] fix a.py"]
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in blocks)
    # a breakpoint after each system block, the tools and the newest message: the four the API allows
    marked = len(blocks) + sum("cache_control" in t for t in payload["tools"])
    marked += sum(isinstance(b, dict) and "cache_control" in b for b in payload["messages"][-1]["content"])
    assert marked == 4


def test_siblings_with_different_tasks_have_the_same_first_block():
    a = anthropic()._payload(msgs("SHARED base", "[agent:worker] task A"), [TOOL], "m", max_tokens=1, temperature=None)
    b = anthropic()._payload(msgs("SHARED base", "[agent:worker] task B"), [TOOL], "m", max_tokens=1, temperature=None)
    assert a["system"][0] == b["system"][0] and a["system"][1] != b["system"][1]
    assert a["tools"] == b["tools"]


def test_one_system_message_is_still_one_block_and_cache_off_marks_nothing():
    one = anthropic()._payload(msgs("only"), [TOOL], "m", max_tokens=1, temperature=None)
    assert one["system"] == [{"type": "text", "text": "only", "cache_control": {"type": "ephemeral"}}]
    off = anthropic(prompt_cache="off")._payload(msgs("a", "b"), [TOOL], "m", max_tokens=1, temperature=None)
    assert off["system"] == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


def test_chat_completions_providers_get_one_system_message():
    wire = messages_to_openai(msgs("SHARED base", "[agent:worker] fix a.py"))
    assert [m["role"] for m in wire] == ["system", "user"]
    assert wire[0]["content"] == "SHARED base\n\n[agent:worker] fix a.py"
    provider = OpenAICompatProvider(name="o", base_url="https://o.test/v1", api_key="k")
    assert provider._payload(msgs("a", "b"), [], "gpt-4o", max_tokens=1, temperature=None)["messages"][0] == {
        "role": "system",
        "content": "a\n\nb",
    }
    later = [*msgs("a", "b"), Message(role="system", content="a note later in the conversation")]
    wire = messages_to_openai(later)
    # only the leading run merges; a note later in the conversation is a user reminder, not a second system message
    assert [m["role"] for m in wire] == ["system", "user", "user"]
    assert "a note later in the conversation" in wire[-1]["content"]


class Seen:
    name, base_url = "seen", "https://seen.test"

    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.requests.append(list(messages))
        yield StreamEvent(type="done", message=Message(role="assistant", content="ok"), usage=Usage())

    async def aclose(self):
        pass


async def test_the_loop_sends_the_suffix_as_a_second_system_message(tmp_path):
    seen = Seen()
    loop = AgentLoop(
        Router(build_chain([seen], [["m"]]), max_retries=0),
        system_prompt="SHARED",
        system_suffix="OWN",
        cwd=tmp_path,
        permission_mode="yolo",
    )
    [e async for e in loop.run("hi")]
    assert [(m.role, m.content) for m in seen.requests[0]] == [("system", "SHARED"), ("system", "OWN"), ("user", "hi")]
    plain = Seen()
    solo = AgentLoop(Router(build_chain([plain], [["m"]]), max_retries=0), system_prompt="SHARED", cwd=tmp_path)
    [e async for e in solo.run("hi")]
    assert [m.role for m in plain.requests[0]] == ["system", "user"]


def test_a_one_hour_cache_is_priced_at_twice_the_input_for_its_writes():
    cfg = Settings()
    five = Usage(prompt_tokens=1_000_000, cache_creation_tokens=1_000_000, model="claude-sonnet-5-5")
    hour = Usage(
        prompt_tokens=1_000_000, cache_creation_tokens=1_000_000, model="claude-sonnet-5-5", cache_write_multiple=2.0
    )
    assert estimate_usd(cfg, five) == 2.0 * 1.25
    assert estimate_usd(cfg, hour) == 2.0 * 2.0
