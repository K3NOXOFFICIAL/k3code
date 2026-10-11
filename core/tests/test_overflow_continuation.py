"""A context overflow in the middle of a turn: the work done so far is kept and the turn goes on from it."""

from __future__ import annotations

from k3code.gateway.server import COMPACTED_CONTINUATION
from k3code.providers.base import ProviderError
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.router import Router, build_chain
from k3code.session_ai import SUMMARY_PREFIX
from m1cmd_helpers import make_server, new_session
from test_auto_compaction import long_history


class Scripted:
    """Call 1 reads a file; the first request that carries the read's result overflows (once); then it answers."""

    name = "fake"
    base_url = "fake://"

    def __init__(self, path: str) -> None:
        self.path, self.calls, self.overflowed = path, [], False

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.calls.append(list(messages))
        if messages and "summarize" in str(messages[0].content).lower() and not tools:
            reply = Message(role="assistant", content="a short summary")
        elif not any(m.role == "tool" for m in messages):
            call = ToolCall(id="c1", name="read", arguments={"path": self.path})
            reply = Message(role="assistant", content=None, tool_calls=[call])
        elif not self.overflowed:
            self.overflowed = True
            raise ProviderError("maximum context length is 1000 tokens, however you requested 9000", status_code=400)
        else:
            reply = Message(role="assistant", content="finished")
        yield StreamEvent(type="done", message=reply, usage=Usage())

    async def aclose(self) -> None:
        return None


async def test_tool_calls_made_before_the_overflow_are_kept_and_the_turn_continues(tmp_path, monkeypatch):
    target = tmp_path / "notes.txt"
    target.write_text("the answer is 42\n")
    server, _ = make_server(
        tmp_path, monkeypatch, ["unused"], context={"compact_at_tokens": 10_000_000, "keep_messages": 6}
    )
    provider = Scripted(str(target))
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    server._tiers = None
    live = server.live[await new_session(server, tmp_path)]
    live.stored.messages = long_history()
    status, text = await server._run_turn(live, "read notes.txt and tell me the answer")
    assert status == "done", (text, live.last_error)
    msgs = live.stored.messages
    assert any(str(m["content"]).startswith(SUMMARY_PREFIX) for m in msgs)
    # the prompt, the read and its result survive once; they were not run a second time
    assert [m["content"] for m in msgs if m["content"] == "read notes.txt and tell me the answer"] == [
        "read notes.txt and tell me the answer"
    ]
    assert sum(1 for m in msgs if m["role"] == "tool") == 1
    assert COMPACTED_CONTINUATION in [m["content"] for m in msgs if m["role"] == "user"]
    assert msgs[-1]["content"] == "finished"
    # the read was requested once: 1 read call + 1 overflowing request + 1 summary + 1 final answer
    assert (
        sum(
            1
            for c in provider.calls
            if not any(m.role == "tool" for m in c) and c[-1].role == "user" and "notes.txt" in str(c[-1].content)
        )
        == 1
    )
    await server.close()


async def test_an_overflow_before_any_tool_ran_reruns_the_prompt_as_before(tmp_path, monkeypatch):
    from test_auto_compaction import Recording, seed

    provider = Recording(overflow_over=6000)
    server, live = await seed(
        tmp_path, monkeypatch, provider, context={"compact_at_tokens": 10_000_000, "keep_messages": 6}
    )
    status, _ = await server._run_turn(live, "NEWPROMPT please")
    assert status == "done"
    assert COMPACTED_CONTINUATION not in [m["content"] for m in live.stored.messages]
    await server.close()
