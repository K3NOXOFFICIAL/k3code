"""What the model is sent is clipped, what the session keeps is not, and consecutive requests share a stable prefix.

A long tool result reaches the provider as its head and tail with the number of dropped chars in between, while the
transcript on disk keeps every byte the tool returned. Requests of one run start with the same bytes as the request
before them, so provider prompt caches can match the prefix.
"""

from __future__ import annotations

from types import SimpleNamespace

from k3code import prompting
from k3code.agent.loop import AgentLoop
from k3code.gateway.server import _estimate_tokens
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage, messages_to_openai
from k3code.reliability import Reliability
from k3code.router import Router, build_chain
from k3code.tools import clip_head_tail, clip_tool_results


class Recorder:
    """Plays one scripted reply per request and keeps what each request carried (messages and tool specs)."""

    name = "fake"
    base_url = "https://fake.test"

    def __init__(self, replies: list[list[StreamEvent]]) -> None:
        self.replies = replies
        self.requests: list[tuple[list[Message], list[tuple[str, str, str]]]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.requests.append((list(messages), [(t.name, t.description, str(t.parameters)) for t in tools]))
        for event in self.replies[len(self.requests) - 1]:
            yield event

    async def aclose(self) -> None:
        return None


def tool_reply(call: ToolCall) -> list[StreamEvent]:
    msg = Message(role="assistant", content=None, tool_calls=[call])
    return [StreamEvent(type="tool_call", tool_call=call), StreamEvent(type="done", message=msg, usage=Usage())]


def text_reply(text: str) -> list[StreamEvent]:
    msg = Message(role="assistant", content=text, tool_calls=[])
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", message=msg, usage=Usage())]


def make_loop(tmp_path, provider: Recorder, session: str) -> AgentLoop:
    return AgentLoop(
        Router(build_chain([provider], [["fake-model"]]), max_retries=0),
        system_prompt="You are a test agent.",
        max_turns=10,
        permission_mode="yolo",
        cwd=tmp_path,
        session=session,
        # the journal and the transcript go under tmp_path, never the real K3CODE_HOME
        reliability=Reliability.from_settings(None, session=session, home=tmp_path / "home"),
    )


def write_big_file(path) -> str:
    path.write_text("".join(f"row {i:05d} {'x' * 40}\n" for i in range(600)))  # about 30 kB
    return str(path)


def test_clip_keeps_the_head_and_the_tail_and_states_how_many_chars_were_dropped():
    text = "".join(f"line {i}\n" for i in range(5000))
    clipped = clip_head_tail(text, 1000)
    head, tail = 600, 400
    dropped = len(text) - head - tail
    assert clipped.startswith(text[:head])
    assert clipped.endswith(text[-tail:])
    assert f"truncated {dropped} chars" in clipped
    assert f"of {len(text)}" in clipped
    assert len(clipped) - (head + tail) < 200  # the marker is the only addition


def test_text_within_the_limit_is_returned_unchanged():
    assert clip_head_tail("x" * 10_000) == "x" * 10_000
    short = [Message(role="tool", content="y" * 10_000, tool_call_id="c1", name="read")]
    assert clip_tool_results(short)[0] is short[0]


def test_only_long_tool_results_are_clipped_and_the_input_is_left_alone():
    big = "a" * 30_000 + "THE-END"
    user = Message(role="user", content=big)
    tool = Message(role="tool", content=big, tool_call_id="c1", name="read")
    original = [user, tool]
    out = clip_tool_results(original)
    assert out[0] is user  # user text is never clipped
    assert out[1].content == clip_head_tail(big)
    assert out[1].content.endswith("THE-END")
    assert tool.content == big  # the input messages are untouched
    assert clip_tool_results(original)[1].content == out[1].content  # the same history gives the same bytes


async def test_the_model_gets_the_clipped_result_and_the_transcript_keeps_the_full_one(tmp_path):
    target = write_big_file(tmp_path / "big.txt")
    provider = Recorder([tool_reply(ToolCall(id="c1", name="read", arguments={"path": target})), text_reply("done")])
    loop = make_loop(tmp_path, provider, session="clip")
    async for _ in loop.run("read the file"):
        pass
    full = next(m.content for m in loop.turn_messages if m.role == "tool")
    assert len(full) > 10_000
    sent = next(m.content for m in provider.requests[1][0] if m.role == "tool")
    assert sent == clip_head_tail(full)
    assert f"truncated {len(full) - 10_000} chars" in sent
    stored = [m.content for m in loop.reliability.load_transcript() if m.role == "tool"]
    assert stored == [full]  # the transcript on disk keeps every char the tool returned


async def test_each_request_starts_with_the_exact_bytes_of_the_one_before(tmp_path):
    big = write_big_file(tmp_path / "big.txt")
    small = tmp_path / "small.txt"
    small.write_text("hello\n")
    provider = Recorder(
        [
            tool_reply(ToolCall(id="c1", name="read", arguments={"path": big})),
            tool_reply(ToolCall(id="c2", name="read", arguments={"path": str(small)})),
            text_reply("done"),
        ]
    )
    loop = make_loop(tmp_path, provider, session="prefix")
    async for _ in loop.run("read both"):
        pass
    (m0, t0), (m1, t1), (m2, t2) = provider.requests
    assert t0 == t1 == t2  # same tools, same schemas, same order
    assert m0[0].role == "system"
    assert m1[: len(m0)] == m0
    assert m2[: len(m1)] == m1


def test_the_volatile_part_of_the_system_prompt_comes_last(tmp_path, monkeypatch):
    cfg = SimpleNamespace(skills=SimpleNamespace(roots=[]), output_style="default")
    monkeypatch.setattr(prompting, "skills_prompt", lambda cwd, roots: "## Skills\n\n- deploy: ship it")
    monkeypatch.setattr(prompting, "memory_prompt", lambda cwd: "## User memory\n\nold preference")
    before = prompting.build_system_prompt("BASE PROMPT", cwd=tmp_path, config=cfg)
    monkeypatch.setattr(prompting, "memory_prompt", lambda cwd: "## User memory\n\nnew preference")
    after = prompting.build_system_prompt("BASE PROMPT", cwd=tmp_path, config=cfg)
    assert before.startswith("BASE PROMPT")
    assert before.index("## Skills") < before.index("## User memory")
    cut = before.index("## User memory")
    assert before[:cut] == after[:cut]  # a rewrite of the learned memory changes only its own tail
    assert before != after


def test_compaction_counts_a_tool_result_as_the_model_receives_it():
    big = "z" * 35_000
    tool_msg = {"role": "tool", "content": big, "tool_call_id": "c", "name": "read"}
    assert _estimate_tokens([tool_msg]) == len(clip_head_tail(big)) // 4
    assert _estimate_tokens([{"role": "user", "content": big}]) == 35_000 // 4


async def test_after_compaction_a_turn_sends_the_summary_and_the_tail_not_the_old_history(tmp_path, monkeypatch):
    from test_auto_compaction import Recording, seed

    provider = Recording()
    server, live = await seed(tmp_path, monkeypatch, provider, context={"compact_at_tokens": 2000, "keep_messages": 6})
    await server._run_turn(live, "first request")  # the ~34 kB of old history is folded into a summary here
    provider.sizes.clear()
    await server._run_turn(live, "second request")
    assert provider.sizes and provider.sizes[-1] < 8_000  # the next request does not resend the old exchanges
    await server.close()


class Scripted:
    """Answers each request with what ``choose`` returns for its messages; keeps every request it was sent."""

    name = "fake"
    base_url = "https://fake.test"

    def __init__(self, choose) -> None:
        self.choose = choose
        self.requests: list[list[Message]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.requests.append(list(messages))
        for event in self.choose(messages):
            yield event

    async def aclose(self) -> None:
        return None


def answer(messages: list[Message]) -> list[StreamEvent]:
    last_user = max(i for i, m in enumerate(messages) if m.role == "user")
    if any(m.role == "tool" for m in messages[last_user + 1 :]):
        return text_reply("the notes say hi")
    if messages[last_user].content == "read the notes":
        # a compact argument string, as many models emit it: a turn boundary must not re-spell it
        call = ToolCall(id="c1", name="read", arguments={"path": "notes.txt"}, raw_arguments='{"path":"notes.txt"}')
        return tool_reply(call)
    return text_reply("ok")


async def test_the_wire_prefix_survives_a_turn_boundary_in_the_gateway(tmp_path, monkeypatch):
    """The next turn rebuilds its history from storage; the tool call must still serialize byte-identically."""
    from m1cmd_helpers import make_server, new_session

    (tmp_path / "notes.txt").write_text("hi\n")
    server, _ = make_server(tmp_path, monkeypatch, ["unused"])
    provider = Scripted(answer)
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    server._tiers = None  # type: ignore[attr-defined]
    live = server.live[await new_session(server, tmp_path)]
    assert (await server._run_turn(live, "read the notes"))[0] == "done"
    assert (await server._run_turn(live, "and now?"))[0] == "done"
    await server.close()
    last_of_turn_1 = messages_to_openai(provider.requests[1])  # the request that carried the tool result
    first_of_turn_2 = messages_to_openai(provider.requests[2])
    assert first_of_turn_2[: len(last_of_turn_1)] == last_of_turn_1
