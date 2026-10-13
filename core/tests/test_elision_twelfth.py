"""Twelfth pass: how a request is kept inside the window (elision, the token estimator, the read budget)."""

from __future__ import annotations

import pytest

from k3code.agent.loop import AgentLoop
from k3code.context_budget import (
    ELIDE_KEEP_CALLS,
    PINNED_RESULT_CHARS,
    elide_marker,
    elide_old_results,
    elision_candidates,
    kept_calls,
    text_tokens,
)
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.reliability import Reliability
from k3code.router import Router, build_chain
from test_wire_clip import Recorder, text_reply, tool_reply


def make_loop(tmp_path, provider, *, window: int | None) -> AgentLoop:
    return AgentLoop(
        Router(build_chain([provider], [["fake-model"]]), max_retries=0),
        system_prompt="You are a test agent.",
        max_turns=40,
        permission_mode="yolo",
        cwd=tmp_path,
        session="twelfth",
        reliability=Reliability.from_settings(None, session="twelfth", home=tmp_path / "home"),
        context_window=window,
    )


def parallel(calls: list[ToolCall]) -> list[StreamEvent]:
    msg = Message(role="assistant", content=None, tool_calls=calls)
    return [StreamEvent(type="done", message=msg, usage=Usage())]


def step(call_id: str, tool: str, result: str, **arguments) -> list[Message]:
    return [
        Message(role="assistant", content=None, tool_calls=[ToolCall(id=call_id, name=tool, arguments=arguments)]),
        Message(role="tool", content=result, tool_call_id=call_id, name=tool),
    ]


def filler(n: int, start: int = 0) -> list[Message]:
    msgs: list[Message] = []
    for i in range(start, start + n):
        msgs += step(f"g{i}", "grep", "y" * 400, pattern="x")
    return msgs


def result_of(out: list[Message], call_id: str) -> str:
    return next(m.content for m in out if m.role == "tool" and m.tool_call_id == call_id)


def call_of(out: list[Message], call_id: str) -> ToolCall:
    return next(tc for m in out for tc in m.tool_calls if tc.id == call_id)


# ── a parallel batch of more than six results reaches the model whole ──


async def test_a_parallel_batch_of_ten_reads_is_not_elided_before_the_model_sees_it(tmp_path):
    """A request already past the threshold (earlier reads) and then ten parallel results: only the six newest were
    protected, so the model was first shown four of them as 'earlier read result elided'."""
    earlier = []
    for i in range(12):
        f = tmp_path / f"e{i}.txt"
        f.write_text(f"EARLIER-{i}\n" + ("q" * 59 + "\n") * 60)
        earlier.append(ToolCall(id=f"e{i}", name="read", arguments={"path": str(f)}))
    batch = []
    for i in range(10):
        f = tmp_path / f"f{i}.txt"
        f.write_text(f"FILE-{i}-START\n" + ("q" * 59 + "\n") * 60)
        batch.append(ToolCall(id=f"r{i}", name="read", arguments={"path": str(f)}))
    provider = Recorder([*(tool_reply(c) for c in earlier), parallel(batch), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=40_000)  # the threshold is 20k tokens: 12 reads + the batch pass it
    async for _ in loop.run("read them all"):
        pass
    request = provider.requests[-1][0]
    first_seen = [m for m in provider.requests[13][0] if m.role == "tool" and m.tool_call_id.startswith("r")]
    assert len(first_seen) == 10
    assert all(f"FILE-{m.tool_call_id[1:]}-START" in m.content for m in first_seen), "a result was elided unseen"
    assert any(
        m.content.startswith("[earlier read result elided") for m in request if m.role == "tool"
    )  # the older ones


def test_the_new_results_of_a_step_are_protected_up_to_a_share_of_the_window():
    asst = Message(role="assistant", content=None)

    def tool(i: int, chars: int) -> Message:
        return Message(role="tool", content="x" * chars, tool_call_id=f"t{i}", name="read")

    small = [asst, *(tool(i, 1_000) for i in range(30))]  # 30 results of ~250 tokens: 7.5k of a 32k window
    assert kept_calls(small, 32_000) == 30
    assert kept_calls(small, None) == ELIDE_KEEP_CALLS  # no window: the fixed count
    # a batch that cannot fit (40% of 32k = 12.8k tokens; 10 results of 4k tokens): the newest three, at least the six
    big = [asst, *(tool(i, 16_000) for i in range(10))]
    assert kept_calls(big, 32_000) == ELIDE_KEEP_CALLS
    # results behind an earlier assistant message are old: only the budget keeps them
    assert kept_calls([*small, asst, tool(99, 1_000)], 32_000) < 30


# ── arguments of a failed call stay ──


def test_the_text_of_a_failed_write_or_edit_is_not_elided_because_it_is_in_no_file():
    big = "def f():\n    return 1\n" * 200
    msgs = [Message(role="user", content="build it")]
    msgs += step("w1", "write", "Error: permission denied: /ro/app.py", path="/ro/app.py", content=big)
    msgs += step("w2", "write", "Wrote /tmp/ok.py", path="/tmp/ok.py", content=big)
    msgs += step("e1", "edit", "Error: old_string not found", path="a.py", old_string=big, new_string=big + "\n")
    msgs += step("e2", "edit", "Error: User denied: edit a.py", path="a.py", old_string=big, new_string=big + "\n")
    msgs += filler(8)
    out = elide_old_results(msgs, over=True, elided=set())
    assert call_of(out, "w1").arguments["content"] == big
    assert (
        call_of(out, "e1").arguments["old_string"] == big and call_of(out, "e2").arguments["new_string"] == big + "\n"
    )
    assert call_of(out, "w2").arguments["content"].startswith("[4400 chars elided")  # a write that worked still is


def test_the_heredoc_of_a_command_that_failed_stays_and_a_reused_call_id_does_not_shield_a_later_call():
    body = "line of the script that is long enough to count\n" * 30
    command = f"cat > /ro/build.sh <<'EOF'\n{body}EOF\nchmod +x /ro/build.sh"
    msgs = [Message(role="user", content="build it")]
    msgs += step("0", "bash", "bash: /ro/build.sh: Permission denied\n[exit code 1]", command=command)
    msgs += step("0", "bash", "(no output)", command=command.replace("/ro/", "./"))  # the same id, and this one worked
    msgs += filler(8)
    out = elide_old_results(msgs, over=True, elided=set())
    first, second = [m for m in out if m.tool_calls and m.tool_calls[0].id == "0"]
    assert first.tool_calls[0].arguments["command"] == command
    assert "chars of text elided" in second.tool_calls[0].arguments["command"]


# ── results a re-run does not give again ──


def test_a_jobs_output_and_an_advisors_answer_are_not_elided_like_old_grep_output():
    msgs = [Message(role="user", content="go")]
    msgs += step("job", "bash_output", "ERROR-LINE " + "x" * 2_800, job_id="job1")
    msgs += step("adv", "advisor", "Use a queue, not a lock. " * 100, question="how?")
    msgs += step("job2", "bash_output", "B" * (PINNED_RESULT_CHARS + 1), job_id="job2")
    msgs += filler(12)
    out = elide_old_results(msgs, over=True, elided=set())
    assert result_of(out, "job").startswith("ERROR-LINE") and result_of(out, "adv").startswith("Use a queue")
    # past the size cap they are elided like the rest, with a marker that does not promise a re-run
    assert result_of(out, "job2").startswith("[earlier bash_output result elided: 12001 chars")
    assert "re-run if needed" not in result_of(out, "job2")


def test_the_marker_of_a_result_that_cannot_be_got_again_does_not_say_to_re_run_it():
    for name in ("read", "grep", "glob"):
        assert elide_marker(name, 900) == f"[earlier {name} result elided: 900 chars — re-run if needed]"
    for name in ("bash_output", "advisor", "bash", "task"):
        marker = elide_marker(name, 900)
        assert marker.startswith(f"[earlier {name} result elided: 900 chars — ") and "re-run if needed" not in marker


# ── a loaded skill is pinned whatever its size ──


@pytest.mark.parametrize("size", [11_000, 13_000, 40_000, 60_000])
def test_a_loaded_skill_is_never_elided_whatever_its_size(size):
    msgs = [Message(role="user", content="use the docx skill")]
    msgs += step("s", "skill", "SKILL INSTRUCTIONS " * (size // 19), name="docx")
    msgs += filler(12)
    out = elide_old_results(msgs, over=True, elided=set())
    assert result_of(out, "s").startswith("SKILL INSTRUCTIONS")
    assert "s" not in {m.tool_call_id for m in elision_candidates(msgs, elided=set())}


# ── the same lesson is not added to the conversation again ──


async def test_a_lesson_the_conversation_already_holds_is_not_added_again(tmp_path):
    """Five turns of the same failure left five identical ``[learned]`` notes in every later request (the loop of a
    turn is a new one: only the history says what was learned)."""
    lesson = "run `ls` on an existing directory first"
    missing = ToolCall(id="m", name="read", arguments={"path": str(tmp_path / "nope.txt")})

    def learned_notes(messages: list[Message]) -> list[str]:
        return [m.content for m in messages if m.role == "system" and (m.content or "").startswith("[learned]")]

    async def turn(history: list[Message]) -> list[Message]:
        provider = Recorder([tool_reply(missing), text_reply("it failed")])
        loop = make_loop(tmp_path, provider, window=None)
        loop.on_tool_outcome = lambda call, result, failure: lesson if failure else None
        async for _ in loop.run("read nope.txt", history=history):
            pass
        return loop.turn_messages

    history: list[Message] = []
    for attempt in range(4):
        messages = await turn(history)
        history = messages[1:]  # a session hands the next turn its conversation without the system prompt
        assert learned_notes(history) == [f"[learned] {lesson}"], attempt
    assert learned_notes(await turn([])) == [f"[learned] {lesson}"]  # a new session still learns


# ── the token estimate of dense text ──


def dense_samples() -> dict[str, str]:
    """Seeded stand-ins for tool output of every density; ``CL100K`` holds what cl100k_base counts for each (measured
    once with tiktoken, which is not a dependency)."""
    import random

    rng = random.Random(20261013)

    def pick(alphabet: str, n: int) -> str:
        return "".join(alphabet[rng.getrandbits(16) % len(alphabet)] for _ in range(n))

    hexa = "0123456789abcdef"
    mixed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    colors, words = ("31", "1;32", "0", "33;1"), ("ERROR", "ok", "warn", "PASS")
    ansi = "".join(f"\x1b[{colors[rng.getrandbits(8) % 4]}m{words[rng.getrandbits(8) % 4]}\x1b[0m " for _ in range(500))
    return {
        "prose": "The quick brown fox jumps over the lazy dog while the committee reviews the results. " * 60,
        "hex": pick(hexa, 6000),
        "base64": pick(mixed + "+/", 8000),
        "uuids": "\n".join("-".join(pick(hexa, k) for k in (8, 4, 4, 4, 12)) for _ in range(150)),
        "ansi": ansi,
        "digits": pick("0123456789", 6000),
        "logs": "\n".join(
            f"2026-10-13T10:{i % 60:02d}:00.123Z INFO worker-{i % 7} "
            f"request id={rng.getrandbits(20)} path=/api/v1/items/{i} status=200"
            for i in range(120)
        ),
        "json": '[{"id":'
        + '},{"id":'.join(f'{i},"name":"item-{i}","tags":["a","b"],"ok":true' for i in range(250))
        + "}]",
        "spaces": " " * 6000,
        "newlines": "\n" * 6000,
        "indent": ("            " * 3 + "x = 1\n") * 300,
        "emoji": "".join("🚀🔥✅🎉👍🙏😀🧪📦"[rng.getrandbits(8) % 9] for _ in range(800)),
        "cyrillic": "Быстрая коричневая лиса прыгает через ленивую собаку, пока комитет рассматривает результаты. "
        * 40,
    }


#: cl100k_base token counts of dense_samples(), measured once with tiktoken (the chars of each in the comment).
CL100K = {
    "prose": 961,  # 5100 chars
    "hex": 3413,  # 6000
    "base64": 5703,  # 8000
    "uuids": 3539,  # 5549
    "ansi": 4492,  # 7212
    "digits": 2000,  # 6000
    "logs": 4322,  # 10800
    "json": 5002,  # 13781
    "spaces": 48,  # 6000
    "newlines": 188,  # 6000
    "indent": 1800,  # 12600
    "emoji": 2218,  # 800
    "cyrillic": 1721,  # 3720
}


@pytest.mark.parametrize("kind", sorted(CL100K))
def test_the_token_estimate_is_within_reach_of_the_real_count_for_every_density_of_text(kind):
    """chars // 4 was 0.35-0.45 of the real count for base64, hex, UUIDs and ANSI colours (the context filled to 91%
    with the estimate at 43%, so elision and compaction never started), 0.62-0.69 for logs and minified JSON, and 8
    to 31 times too large for runs of newlines and spaces."""
    text = dense_samples()[kind]
    ratio = text_tokens(text) / CL100K[kind]
    if kind in ("spaces", "newlines"):  # a run of blanks is a few tokens: far too many is the only way to be wrong
        assert ratio <= 4, ratio
    else:
        assert 0.7 <= ratio <= 1.5, ratio


def test_the_estimate_of_ordinary_text_and_short_text_is_unchanged_in_kind():
    assert text_tokens("") == 0 and text_tokens("echo 5") == 1 and text_tokens("x" * 96) == 24  # chars // 4 up to 96
    assert text_tokens("a" * 4000) < text_tokens("aB1" * 1333)  # alternating case and digits cost more per char
    assert text_tokens("漢" * 1000) >= 1000  # a CJK character is a token or so


def test_the_estimate_does_not_cost_more_for_a_huge_result_than_for_a_big_one():
    """It runs on every message of every request: past 1000 chars it judges three windows of the text."""
    import time

    def cost(text: str) -> float:
        best = float("inf")
        for _ in range(5):
            start = time.perf_counter()
            for _ in range(300):
                text_tokens(text)
            best = min(best, (time.perf_counter() - start) / 300)
        return best

    big = ("line of output 1234\n" * 600)[:10_000]
    assert cost(big * 100) < 3 * cost(big) + 5e-6  # a megabyte costs what ten kilobytes do
    assert cost(big) < 100e-6  # about 4 us here; generous for a slow machine


# ── one tool result must not fill a small window; the request must fit; the reply gets the room that is left ──


def lines_of(n: int, word: str = "committee") -> str:
    return "".join(f"line {i:05d}: the {word} reviewed the quarterly results for item {i}.\n" for i in range(n))


def test_a_read_result_is_held_to_a_share_of_the_window_and_is_unchanged_in_a_big_one():
    from k3code.tools import clip_for_model

    big = "".join(f"{i + 1}\t{line}\n" for i, line in enumerate(lines_of(780).splitlines()))  # a read of ~59 KB
    assert clip_for_model("read", big) == big  # no window: its own 60 000-char budget, as before
    assert clip_for_model("read", big, 10_000, 200_000) == big and clip_for_model("read", big, 10_000, 128_000) == big
    for window in (8_000, 16_000):
        clipped = clip_for_model("read", big, 10_000, window)
        assert text_tokens(clipped) <= window * 0.2 * 1.25, window  # a fifth of the window, the marker on top
        assert "read with offset and limit" in clipped and clipped.startswith("1\tline 00000")
    tiny = clip_for_model("read", big, 10_000, 1_000)  # the floor: a read stays usable in the smallest window
    assert 4_000 <= len(tiny) <= 4_300
    chinese = "".join(
        f"第{i:05d}行：今天我们讨论一下这个项目的整体架构设计以及后续的开发计划，请大家认真阅读文档。\n"
        for i in range(2000)
    )
    for window in (32_000, 64_000):  # CJK text is a token or so a char: 60 000 chars were 58 000 tokens
        assert text_tokens(clip_for_model("read", chinese, 10_000, window)) <= window * 0.2 * 1.25, window
    skill = "SKILL INSTRUCTIONS " * 3_000
    assert clip_for_model("skill", skill, 10_000, 200_000) == skill  # a skill is still followed whole where it fits
    assert len(clip_for_model("skill", skill, 10_000, 8_000)) < len(skill) // 3


async def test_a_big_read_in_a_small_window_does_not_make_the_session_unusable(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text(lines_of(1000))
    window = 8_000
    provider = Recorder([tool_reply(ToolCall(id="r", name="read", arguments={"path": str(f)})), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=window)
    async for _ in loop.run("read big.txt"):
        pass
    request = provider.requests[-1][0]
    assert text_tokens("\n".join(m.content or "" for m in request)) + 1_700 < window  # the tool schemas are ~1.6k
    shown = next(m.content for m in request if m.role == "tool")
    assert "truncated" in shown and "read with offset and limit" in shown
    # the next prompt is sent with the same clipped result and fits too (it used to be 16 140 tokens against 16 000)
    provider.replies.append(text_reply("hello"))
    async for _ in loop.run("hello", history=loop.turn_messages[1:]):
        pass
    assert next(m.content for m in provider.requests[-1][0] if m.role == "tool") == shown


async def test_a_request_still_over_the_window_after_elision_has_its_largest_results_cut_down(tmp_path):
    """Two loaded skills (never elided) at their share of a 5k window, a long prompt and the tool schemas: past 85%."""
    window = 5_000
    loop = make_loop(tmp_path, Recorder([]), window=window)
    specs = loop.tool_specs()
    prompt = "Please follow the skills below and report. " + "and more words " * 330
    msgs = [Message(role="system", content=loop.system_prompt), Message(role="user", content=prompt)]
    msgs += step("s1", "skill", "FIRST SKILL " + lines_of(400, "first"), name="first")
    msgs += step("s2", "skill", "SECOND SKILL " + lines_of(400, "second"), name="second")
    view = await loop._request_messages(msgs, specs)
    cut = [m.content for m in view if m.role == "tool"]
    assert len(cut) == 2 and all("truncated" in c and c.startswith(("FIRST", "SECOND")) for c in cut)
    assert loop._request_used is not None and loop._request_used <= window * 0.85, loop._request_used
    assert {k for k in loop._elided if k.startswith("clip:")} == {"clip:s1", "clip:s2"}
    # the next step: the same results are cut the same way (the prefix of the request does not change again)
    msgs += step("g1", "grep", "y" * 50, pattern="x")
    assert [m.content for m in await loop._request_messages(msgs, specs) if m.name == "skill"] == cut
    # a request that fits is left alone
    small = [Message(role="system", content=""), Message(role="user", content="hi"), *step("s", "skill", "short")]
    assert await make_loop(tmp_path, Recorder([]), window=window)._request_messages(small, specs) == small


class RecordingMax(Recorder):
    """A Recorder that also keeps the ``max_tokens`` of each request."""

    def __init__(self, replies):
        super().__init__(replies)
        self.max_tokens: list[int] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.max_tokens.append(max_tokens)
        async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
            yield event


async def test_max_tokens_is_clamped_to_the_room_the_window_leaves_after_the_request(tmp_path):
    from k3code.context_budget import REPLY_MIN_TOKENS, message_tokens, overhead_tokens

    window = 8_000

    async def sent_max(prompt: str, window: int | None) -> tuple[int, int]:
        provider = RecordingMax([text_reply("ok")])
        loop = make_loop(tmp_path, provider, window=window)
        async for _ in loop.run(prompt, max_tokens=8192):
            pass
        request, specs = provider.requests[0]
        used = message_tokens(request) + overhead_tokens(loop.system_prompt, loop.tool_specs())
        return provider.max_tokens[0], used

    small, _ = await sent_max("hi", window)
    assert small == window // 4  # lots of room: the old cap, a quarter of the window
    unlimited, _ = await sent_max("hi", None)
    assert unlimited == 8192  # no window known: as asked
    prompt = "The committee reviewed the quarterly results and decided that the proposal should be approved. " * 260
    clamped, used = await sent_max(prompt, window)  # ~4.9k tokens of prompt and 1.6k of tool schemas
    assert REPLY_MIN_TOKENS <= clamped < window // 4, clamped
    assert used + clamped <= window, (used, clamped)  # what the provider checks: input and max_tokens together
    crowded, _ = await sent_max(prompt * 2, window)  # the prompt alone leaves nothing: not below the minimum
    assert crowded == REPLY_MIN_TOKENS


async def test_an_empty_reply_that_used_a_limit_the_window_set_is_not_retried_with_a_larger_cap(tmp_path):
    """The window left less room than the cap: a doubled cap is clamped to the same room (an identical paid call), and
    "raise max_tokens" would not help; the notice names the limit the request had and says the window is full."""
    cut = Message(role="assistant", content=None, stop_reason="max_tokens")
    provider = RecordingMax([[StreamEvent(type="done", message=cut, usage=Usage())]] * 2)
    loop = make_loop(tmp_path, provider, window=8_000)
    texts: list[str] = []

    async def collect(delta: str) -> None:
        texts.append(delta)

    loop.on_text_delta = collect
    prompt = "The committee reviewed the quarterly results and decided that the proposal should be approved. " * 260
    async for _ in loop.run(prompt, max_tokens=8192):
        pass
    assert len(provider.max_tokens) == 1 and provider.max_tokens[0] < 2_000, provider.max_tokens
    note = "".join(texts)
    assert f"whole {provider.max_tokens[0]}-token output limit" in note and "/compact" in note
    assert "Raise max_tokens" not in note
