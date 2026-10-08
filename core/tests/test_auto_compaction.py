"""Long sessions compact themselves: a /loop in one session used to grow its history forever."""

from __future__ import annotations

from k3code.autonomy.advisor import SUMMARY_SYSTEM
from k3code.providers.base import ProviderError
from k3code.providers.types import Message, StreamEvent, Usage
from k3code.router import Router, build_chain
from k3code.session_ai import SUMMARY_PREFIX
from m1cmd_helpers import make_server, new_session


class Recording:
    """Answers every call; with `overflow_over` set, a call whose text is longer than that raises a context overflow."""

    name = "fake"
    base_url = "fake://"

    def __init__(self, overflow_over: int | None = None) -> None:
        self.overflow_over = overflow_over
        self.sizes: list[int] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        text = "\n".join(str(m.content or "") for m in messages)
        self.sizes.append(len(text))
        if self.overflow_over is not None and "NEWPROMPT" in text and len(text) > self.overflow_over:
            raise ProviderError(
                "This model's maximum context length is 1000 tokens, however you requested 9000", status_code=400
            )
        reply = "a short summary" if messages and messages[0].content == SUMMARY_SYSTEM else "ok"
        yield StreamEvent(type="text_delta", text=reply)
        yield StreamEvent(type="done", message=Message(role="assistant", content=reply, tool_calls=[]), usage=Usage())

    async def aclose(self) -> None:
        return None


def long_history(n: int = 40) -> list[dict]:
    out = [{"role": "system", "content": "system prompt"}]
    for i in range(n):
        out.append({"role": "user", "content": f"request {i} " + "x" * 400})
        out.append({"role": "assistant", "content": f"answer {i} " + "y" * 400})
    return out


async def seed(tmp_path, monkeypatch, provider, **settings):
    server, _ = make_server(tmp_path, monkeypatch, ["unused"], **settings)
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    server._tiers = None
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    live.stored.messages = long_history()
    server.store.save(live.stored)
    return server, live


async def test_a_large_conversation_is_summarized_before_the_turn(tmp_path, monkeypatch):
    provider = Recording()
    server, live = await seed(tmp_path, monkeypatch, provider, context={"compact_at_tokens": 2000, "keep_messages": 6})
    before = len(live.stored.messages)
    status, _ = await server._run_turn(live, "next request")
    assert status == "done"
    msgs = live.stored.messages
    assert len(msgs) < before // 2, (before, len(msgs))
    assert msgs[0]["role"] == "system" and msgs[1]["content"].startswith(SUMMARY_PREFIX)
    assert msgs[-2]["content"] == "next request" and msgs[-1]["content"] == "ok"  # the turn itself was kept intact
    await server.close()


async def test_a_small_conversation_is_left_alone(tmp_path, monkeypatch):
    provider = Recording()
    server, live = await seed(tmp_path, monkeypatch, provider)  # default threshold: 80,000 tokens
    live.stored.messages = long_history(3)
    await server._run_turn(live, "hi")
    assert not any(str(m["content"]).startswith(SUMMARY_PREFIX) for m in live.stored.messages)
    await server.close()


async def test_a_context_overflow_compacts_and_retries_once(tmp_path, monkeypatch):
    """The provider rejects the long conversation (HTTP 400, context length): fold the history and run the turn again
    instead of failing it - and every later tick of a loop with it."""
    provider = Recording(overflow_over=6000)
    big = {"compact_at_tokens": 10_000_000, "keep_messages": 6}  # proactive compaction off: only the retry fires
    server, live = await seed(tmp_path, monkeypatch, provider, context=big)
    status, text = await server._run_turn(live, "NEWPROMPT please")
    assert status == "done", (text, live.last_error)
    msgs = live.stored.messages
    assert any(str(m["content"]).startswith(SUMMARY_PREFIX) for m in msgs)
    assert [m["content"] for m in msgs if m["content"] == "NEWPROMPT please"] == ["NEWPROMPT please"]  # not duplicated
    assert msgs[-1]["content"] == "ok"
    await server.close()
