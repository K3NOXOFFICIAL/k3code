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


async def test_a_background_note_is_not_lost_when_an_overflow_reruns_the_prompt(tmp_path, monkeypatch):
    from k3code.subagents.runner import Handle
    from test_auto_compaction import Recording, seed

    prompts: list[str] = []

    class Noting(Recording):
        async def stream(self, messages, tools, model, **kw):
            users = [str(m.content) for m in messages if m.role == "user"]
            if users and "NEWPROMPT" in users[-1]:
                prompts.append(users[-1])
            async for event in super().stream(messages, tools, model, **kw):
                yield event

    provider = Noting(overflow_over=6000)
    server, live = await seed(
        tmp_path, monkeypatch, provider, context={"compact_at_tokens": 10_000_000, "keep_messages": 6}
    )
    h = Handle(id="sa-1", description="d", agent_type="worker", tier="main", depth=1, parent_sid=live.session_id)
    h.status, h.result, h.background = "completed", "BGRESULT", True
    server.subagents.handles["sa-1"] = h
    status, text = await server._run_turn(live, "NEWPROMPT please")
    assert status == "done", (text, live.last_error)
    assert len(prompts) == 2 and all("BGRESULT" in p for p in prompts), "the rerun carries the note as well"
    assert h.reported, "and the model has answered it by now"
    await server.close()


async def test_a_background_note_is_not_lost_when_the_window_ran_out_under_the_reply(tmp_path, monkeypatch):
    """The first reply is cut off by the window (stop reason model_context_window_exceeded): the loop yields it, then
    raises the overflow, and the rerun builds its prompt again. The note was marked reported on that cut reply."""
    from k3code.subagents.runner import Handle
    from test_auto_compaction import Recording, seed

    prompts: list[str] = []

    class Cut(Recording):
        cut = False

        async def stream(self, messages, tools, model, **kw):
            users = [str(m.content) for m in messages if m.role == "user"]
            if users and "NEWPROMPT" in users[-1]:
                prompts.append(users[-1])
                if not self.cut:
                    self.cut = True
                    reply = Message(role="assistant", content="par", stop_reason="model_context_window_exceeded")
                    yield StreamEvent(type="done", message=reply, usage=Usage())
                    return
            async for event in super().stream(messages, tools, model, **kw):
                yield event

    server, live = await seed(
        tmp_path, monkeypatch, Cut(), context={"compact_at_tokens": 10_000_000, "keep_messages": 6}
    )
    h = Handle(id="sa-1", description="d", agent_type="worker", tier="main", depth=1, parent_sid=live.session_id)
    h.status, h.result, h.background = "completed", "BGRESULT", True
    server.subagents.handles["sa-1"] = h
    status, text = await server._run_turn(live, "NEWPROMPT please")
    assert status == "done", (text, live.last_error)
    assert len(prompts) == 2 and all("BGRESULT" in p for p in prompts), "the rerun carries the note as well"
    assert h.reported, "and the model has answered it by now"
    await server.close()


async def test_a_background_note_is_not_marked_reported_when_a_failed_fan_out_replaces_the_prompt(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from k3code.subagents.runner import Handle
    from test_auto_compaction import Recording, seed

    prompts: list[str] = []

    class Noting(Recording):
        async def stream(self, messages, tools, model, **kw):
            users = [str(m.content) for m in messages if m.role == "user"]
            if users and "ESCALATED" in users[-1]:
                prompts.append(users[-1])
            async for event in super().stream(messages, tools, model, **kw):
                yield event

    server, live = await seed(tmp_path, monkeypatch, Noting(), context={"compact_at_tokens": 10_000_000})
    h = Handle(id="sa-1", description="d", agent_type="worker", tier="main", depth=1, parent_sid=live.session_id)
    h.status, h.result, h.background = "completed", "BGRESULT", True
    server.subagents.handles["sa-1"] = h

    async def failed_run(session, text, plan, subtasks):  # the children could not finish: the parent gets this prompt
        return SimpleNamespace(ok=False, escalation_prompt=lambda task: f"ESCALATED: finish {task}")

    server.fanout.applies = lambda session, gate: [object()]  # type: ignore[method-assign]
    server.fanout.run = failed_run  # type: ignore[method-assign]
    status, text = await server._run_turn(live, "do the big task")
    assert status == "done", (text, live.last_error)
    assert len(prompts) == 1 and "BGRESULT" not in prompts[0]
    assert not h.reported, "no prompt the model answered carried the note: it is shown on the next turn"
    await server.close()


async def test_stop_during_the_overflow_summary_keeps_the_prompt_and_the_tool_work(tmp_path, monkeypatch):
    import asyncio

    target = tmp_path / "notes.txt"
    target.write_text("the answer is 42\n")
    started = asyncio.Event()

    class Hanging(Scripted):
        async def stream(self, messages, tools, model, **kw):
            if messages and "summarize" in str(messages[0].content).lower() and not tools:
                started.set()
                await asyncio.sleep(3600)  # the summary call never answers
            async for event in super().stream(messages, tools, model, **kw):
                yield event

    server, _ = make_server(
        tmp_path, monkeypatch, ["unused"], context={"compact_at_tokens": 10_000_000, "keep_messages": 6}
    )
    provider = Hanging(str(target))
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    server._tiers = None
    live = server.live[await new_session(server, tmp_path)]
    live.stored.messages = long_history()
    task = asyncio.create_task(server._run_turn(live, "read notes.txt and tell me the answer"))
    await asyncio.wait_for(started.wait(), timeout=20)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    msgs = live.stored.messages
    assert "read notes.txt and tell me the answer" in [m["content"] for m in msgs if m["role"] == "user"]
    assert sum(1 for m in msgs if m["role"] == "tool") == 1
    await server.close()


class Looping:
    """Always asks for another read of ``target``; its third request overflows once."""

    name, base_url = "fake", "fake://"

    def __init__(self, target) -> None:
        self.target = target
        self.requests = 0  # model requests answered (the summary call and the refused request do not count)

    async def stream(self, messages, tools, model, **kw):
        if messages and "summarize" in str(messages[0].content).lower() and not tools:
            reply = Message(role="assistant", content="a short summary")
        else:
            n = self.requests + 1
            if n == 3 and not getattr(self, "refused", False):
                self.refused = True
                raise ProviderError("maximum context length is 1000 tokens, however you requested 9000", 400)
            self.requests += 1
            call = ToolCall(id=f"c{self.requests}", name="read", arguments={"path": str(self.target), "offset": n})
            reply = Message(role="assistant", content=None, tool_calls=[call])
        yield StreamEvent(type="done", message=reply, usage=Usage())

    async def aclose(self) -> None:
        return None


async def looping_turn(tmp_path, monkeypatch, max_turns: int):
    target = tmp_path / "notes.txt"
    target.write_text("".join(f"line {i}\n" for i in range(50)))
    server, _ = make_server(
        tmp_path, monkeypatch, ["unused"], context={"compact_at_tokens": 10_000_000, "keep_messages": 4}
    )
    server.config.max_turns = max_turns
    provider = Looping(target)
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    server._tiers = None
    live = server.live[await new_session(server, tmp_path)]
    live.stored.messages = long_history()
    status, text = await server._run_turn(live, "keep reading")
    await server.close()
    return provider, status, text


async def test_the_max_turns_cap_covers_the_whole_task_across_an_overflow_continuation(tmp_path, monkeypatch):
    provider, _, _ = await looping_turn(tmp_path, monkeypatch, max_turns=4)
    assert provider.requests == 4, "two before the overflow and the two that are left of the four, not four more"


async def test_the_stop_message_after_an_overflow_continuation_names_the_configured_cap(tmp_path, monkeypatch):
    """The loop of the continuation was handed the calls left (4 - 2) as its cap, and the stop message reported that."""
    provider, status, text = await looping_turn(tmp_path, monkeypatch, max_turns=4)
    assert status == "needs_input"
    assert "I stopped after 4 model calls" in text and "`max_turns` cap (4)" in text, text


async def test_the_continuation_does_not_run_the_prompt_hooks_or_the_scope_gate_again(tmp_path, monkeypatch):
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
    calls = {"prepare": 0, "hooks": 0}
    prepare, hooks = server.autonomy.prepare, server._prompt_hooks

    async def counting_prepare(*args, **kwargs):
        calls["prepare"] += 1
        return await prepare(*args, **kwargs)

    async def counting_hooks(*args, **kwargs):
        calls["hooks"] += 1
        return await hooks(*args, **kwargs)

    server.autonomy.prepare = counting_prepare  # type: ignore[method-assign]
    server._prompt_hooks = counting_hooks  # type: ignore[method-assign]
    status, _ = await server._run_turn(live, "read notes.txt and tell me the answer")
    assert status == "done" and COMPACTED_CONTINUATION in [m["content"] for m in live.stored.messages]
    assert calls["prepare"] == 1, "the scope gate ran for the prompt, not again for its continuation"
    assert calls["hooks"] <= 1
    await server.close()
