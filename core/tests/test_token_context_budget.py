"""History weight: per-model context windows drive compaction, old tool results are elided from big requests (never
from stored history), and a re-read of an unchanged file in the same turn is answered with a pointer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from k3code.agent.loop import AgentLoop
from k3code.context_budget import compact_threshold, context_window
from k3code.gateway.server import _estimate_tokens
from k3code.providers.types import Message, ToolCall
from k3code.reliability import Reliability
from k3code.router import Router, build_chain
from k3code.routing.tiers import TaskKind
from test_auto_compaction import Recording, long_history, seed
from test_wire_clip import Recorder, text_reply, tool_reply


def test_context_windows_by_config_and_by_family():
    cfg = SimpleNamespace(models={"my-local": {"context_window": 65_536}}, context={})
    assert context_window(cfg, "my-local") == 65_536
    assert context_window(cfg, "claude-sonnet-4-5") == 200_000
    assert context_window(cfg, "anthropic/claude-haiku") == 200_000
    assert context_window(cfg, "gpt-4o-mini") == 128_000
    assert context_window(cfg, "gpt-4.1") == 128_000
    assert context_window(cfg, "llama-3-8b") == 128_000  # no known family: the fallback
    assert compact_threshold(cfg, "claude-x") == 140_000  # 0.7 of the window
    cfg.context = {"compact_at_ratio": 0.5}
    assert compact_threshold(cfg, "gpt-4o") == 64_000
    cfg.context = {"compact_at_tokens": 5000, "compact_at_ratio": 0.5}
    assert compact_threshold(cfg, "gpt-4o") == 5000  # the absolute value overrides


def test_an_unknown_id_such_as_a_gateway_alias_is_not_compacted_at_22k():
    """Issue #30: ids with no known family got 32k, so a gateway alias in front of a large model compacted at ~22k."""
    cfg = SimpleNamespace(models={}, context={})
    assert context_window(cfg, "auto/coding-manual") == 128_000
    assert context_window(cfg, "auto") == 128_000
    assert compact_threshold(cfg, "auto/coding-manual") == 89_600
    # the Claude Code aliases the claude-cli provider sends are Claude models
    assert [context_window(cfg, m) for m in ("sonnet", "opus", "haiku")] == [200_000] * 3
    # an explicit entry still wins, also below the fallback
    cfg.models = {"auto": {"context_window": 16_000}}
    assert context_window(cfg, "auto") == 16_000


async def test_the_active_models_window_decides_when_a_session_is_compacted(tmp_path, monkeypatch):
    provider = Recording()
    server, live = await seed(tmp_path, monkeypatch, provider, context={"keep_messages": 6})
    model = server._active_model(live)
    server.config.models = {model: {"context_window": 1_000_000}}  # ~8k tokens of history: far below 70%
    assert await server._maybe_compact(live) == 0
    server.config.models = {model: {"context_window": 10_000}}  # 70% = 7000 tokens: over it
    assert await server._maybe_compact(live) > 0
    await server.close()


async def test_the_estimate_counts_the_system_prompt_and_the_tool_schemas(tmp_path, monkeypatch):
    provider = Recording()
    server, live = await seed(tmp_path, monkeypatch, provider, context={"keep_messages": 6})
    live.stored.messages = long_history(10)  # ~2k tokens of conversation
    conversation = _estimate_tokens([m for m in live.stored.messages if m["role"] != "system"])
    overhead = server._request_tokens(live) - conversation
    assert overhead > 500  # the system prompt and the built-in tool schemas
    window = int((conversation + overhead // 2) / 0.7)  # the conversation alone stays under 70%, with them it passes
    server.config.models = {server._active_model(live): {"context_window": window}}
    assert await server._maybe_compact(live) > 0
    await server.close()


def make_loop(tmp_path, provider, *, window: int | None) -> AgentLoop:
    return AgentLoop(
        Router(build_chain([provider], [["fake-model"]]), max_retries=0),
        system_prompt="You are a test agent.",
        max_turns=20,
        permission_mode="yolo",
        cwd=tmp_path,
        session="budget",
        reliability=Reliability.from_settings(None, session="budget", home=tmp_path / "home"),
        context_window=window,
    )


def reads_of(tmp_path, n: int, lines: int = 50) -> list[ToolCall]:
    calls = []
    for i in range(n):
        f = tmp_path / f"f{i}.txt"
        f.write_text(f"file {i}\n" + ("q" * 59 + "\n") * lines)  # ~3 kB
        calls.append(ToolCall(id=f"r{i}", name="read", arguments={"path": str(f)}))
    return calls


async def run_reads(tmp_path, window: int | None) -> tuple[AgentLoop, Recorder]:
    calls = reads_of(tmp_path, 9)
    provider = Recorder([*(tool_reply(c) for c in calls), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=window)
    async for _ in loop.run("read them all"):
        pass
    return loop, provider


async def test_old_tool_results_are_elided_from_a_big_request_but_kept_in_history(tmp_path):
    loop, provider = await run_reads(tmp_path, window=8_000)  # 50% = 4000 tokens; 9 results are ~7k tokens
    last_request = provider.requests[-1][0]
    sent = {m.tool_call_id: m.content for m in last_request if m.role == "tool"}
    for i in range(3):
        assert sent[f"r{i}"].startswith("[earlier read result elided: ")
        assert "re-run if needed]" in sent[f"r{i}"]
    # the newest results are kept whole up to 15% of the window (1200 tokens: one result of ~750 here), not a fixed six
    whole = [i for i in range(9) if f"file {i}" in sent[f"r{i}"]]
    assert 8 in whole and len(whole) <= 3 and len(sent["r8"]) > 3000
    stored = [m.content for m in loop.turn_messages if m.role == "tool"]
    assert all(len(c) > 3000 for c in stored)  # the conversation itself keeps every result
    transcript = [m.content for m in loop.reliability.load_transcript() if m.role == "tool"]
    assert all(len(c) > 3000 for c in transcript)


async def test_six_results_stay_whole_when_they_are_small_next_to_the_window(tmp_path):
    calls = reads_of(tmp_path, 12, lines=8)  # ~500 chars each
    provider = Recorder([*(tool_reply(c) for c in calls), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=6_000)  # overhead + 12 results pass 50%; 6 results are < 15%
    async for _ in loop.run("read them all"):
        pass
    sent = {m.tool_call_id: m.content for m in provider.requests[-1][0] if m.role == "tool"}
    assert sent["r0"].startswith("[earlier read result elided: ")
    assert all(f"file {i}" in sent[f"r{i}"] for i in range(6, 12))


def test_the_kept_count_never_falls_below_the_latest_step_and_only_grows_forward():
    from k3code.context_budget import kept_calls

    def tool(i: int, chars: int) -> Message:
        return Message(role="tool", content="x" * chars, tool_call_id=f"t{i}", name="read")

    asst = Message(role="assistant", content=None)
    huge = [asst, *(tool(i, 40_000) for i in range(8))]  # one step of 8 parallel results, 10k tokens each
    assert kept_calls(huge, 32_000) == 6  # the model has not seen them yet: the cap, not 1
    assert kept_calls([asst, tool(0, 40_000)], 32_000) == 1  # always one
    assert kept_calls([asst, tool(0, 100)], None) == 6  # no window: the fixed count
    # a lesson, a loop-guard note or a steering message appended behind the results does not make them old
    step = [asst, *(tool(i, 40_000) for i in range(3))]
    assert kept_calls(step, 32_000) == 3
    assert kept_calls([*step, Message(role="system", content="note")], 32_000) == 3
    assert kept_calls([*step, Message(role="user", content="stop that")], 32_000) == 3
    assert kept_calls([*step, asst, Message(role="user", content="next")], 32_000) == 1  # a later step: only the budget
    # boundary of the oldest kept result never moves back as results are appended
    msgs: list[Message] = []
    boundary = 0
    for i in range(30):
        msgs += [asst, tool(i, 3_000 if i % 3 else 30_000)]
        oldest_kept = len([m for m in msgs if m.role == "tool"]) - kept_calls(msgs, 30_000)
        assert oldest_kept >= boundary
        boundary = oldest_kept


def test_a_result_the_session_elided_stays_elided_when_a_larger_window_keeps_more_whole():
    from k3code.context_budget import elide_old_results

    msgs = []
    for i in range(10):
        msgs += [
            Message(role="assistant", content=None),
            Message(role="tool", content=f"file {i} " + "o" * 4000, tool_call_id=f"c{i}", name="read"),
        ]
    elided: set[str] = set()
    small = elide_old_results(msgs, over=True, elided=elided, window=8_000)  # keeps one result whole
    assert [m.content.startswith("[earlier") for m in small if m.role == "tool"].count(True) == 9
    big = elide_old_results(msgs, over=False, elided=elided, window=200_000)  # the same session, a bigger model
    assert all(m.content.startswith("[earlier") for m in big if m.role == "tool" and m.tool_call_id != "c9")


def test_results_of_a_few_hundred_chars_are_elided_too():
    from k3code.context_budget import ELIDE_MIN_CHARS, elide_old_results

    msgs = []
    for i in range(10):
        msgs += [
            Message(role="assistant", content=None),
            Message(role="tool", content="o" * (ELIDE_MIN_CHARS + 50), tool_call_id=f"t{i}", name="bash"),
        ]
    msgs.append(Message(role="tool", content="tiny", tool_call_id="tiny", name="bash"))
    out = elide_old_results(msgs, over=True, elided=set(), window=None)
    assert out[1].content.startswith("[earlier bash result elided")
    assert out[-1].content == "tiny"


async def test_elision_runs_in_batches_so_the_request_prefix_rarely_changes(tmp_path):
    """Past half the window every step used to elide the result that had just left the last six: each call rewrote
    the request prefix (a prompt-cache miss; a persistent claude-cli process no longer mirrored it and was started
    afresh). Now more is elided only once the request is over the threshold again, and then every old result at once."""
    calls = reads_of(tmp_path, 40)
    provider = Recorder([*(tool_reply(c) for c in calls), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=30_000)
    loop.max_turns = 0  # make_loop caps at 20 model calls
    async for _ in loop.run("read them all"):
        pass
    sent = [[m.content for m in request[0]] for request in provider.requests]
    rewrites = sum(1 for before, after in zip(sent, sent[1:], strict=False) if after[: len(before)] != before)
    assert 1 <= rewrites <= 5  # one per batch (three here); it was one per step (24)
    last = {m.tool_call_id: m.content for m in provider.requests[-1][0] if m.role == "tool"}
    assert last["r0"].startswith("[earlier read result elided: ")
    assert all(f"file {i}" in last[f"r{i}"] for i in range(34, 40))  # the last six stay whole


async def test_nothing_is_elided_below_half_the_window(tmp_path):
    _, provider = await run_reads(tmp_path, window=200_000)
    assert not any("elided" in (m.content or "") for m in provider.requests[-1][0])


async def test_a_reread_of_an_unchanged_file_points_at_the_earlier_result(tmp_path):
    f = tmp_path / "same.txt"
    f.write_text("one\ntwo\nthree\n")
    read = {"path": str(f)}
    script = [
        ToolCall(id="a", name="read", arguments=read),
        ToolCall(id="b", name="read", arguments=read),  # unchanged: a pointer
        ToolCall(id="c", name="read", arguments={"path": str(f), "offset": 2}),  # another range: read it
        ToolCall(id="d", name="write", arguments={"path": str(f), "content": "one\nTWO!\n"}),
        ToolCall(id="e", name="read", arguments=read),  # changed: read it again
    ]
    provider = Recorder([*(tool_reply(c) for c in script), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=None)
    async for _ in loop.run("read it twice"):
        pass
    results = {m.tool_call_id: m.content for m in loop.turn_messages if m.role == "tool"}
    assert "\ttwo" in results["a"]
    assert results["b"].startswith("[unchanged since the read at step 1 (call a)")
    assert "\ttwo" not in results["b"]
    assert results["c"].startswith("     2\ttwo")
    assert "TWO!" in results["e"]


async def test_a_loop_takes_its_window_from_the_models_its_router_sends_to(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={})
    main = server._active_model(live)
    server.config.models = {main: {"context_window": 200_000}, "tiny-cheap": {"context_window": 8_000}}
    rel = Reliability.from_settings(None, session="w", home=tmp_path / "home")
    cheap = Router(build_chain([Recording()], [["tiny-cheap"]]), max_retries=0)
    loop = server._build_loop(live, rel, cheap, TaskKind.INTERACTIVE_TURN, None)
    assert loop.context_window == 8_000  # not the main model's 200k
    mixed = Router(build_chain([Recording()], [["tiny-cheap", main]]), max_retries=0)
    assert server._build_loop(live, rel, mixed, TaskKind.INTERACTIVE_TURN, None).context_window == 8_000
    await server.close()


async def test_the_active_model_honours_tiers_main(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={})
    server.config.providers[0].tiers = {"main": "tier-main-model"}
    live.stored.model = None
    server.config.default_model = "default"
    assert server._active_model(live) == "tier-main-model"
    await server.close()


# ── arguments that are elsewhere by now ───────────────────────────────────────────────────────────────────────────


def _call_message(i: int, name: str, args: dict) -> Message:
    return Message(role="assistant", content=None, tool_calls=[ToolCall(id=f"k{i}", name=name, arguments=args)])


def _result(i: int, name: str, text: str = "ok") -> Message:
    return Message(role="tool", content=text, tool_call_id=f"k{i}", name=name)


def test_a_cut_off_calls_half_written_arguments_do_not_ride_along_in_every_request():
    from k3code.context_budget import CUT_CALL_KEEP_CHARS, shrink_cut_calls

    cut = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="c1", name="write", arguments={"_unparsed": "x" * 30_000}, raw_arguments="y" * 30_000)],
    )
    ok = Message(
        role="assistant", content=None, tool_calls=[ToolCall(id="c2", name="bash", arguments={"command": "ls"})]
    )
    out = shrink_cut_calls([cut, ok])
    shown = out[0].tool_calls[0]
    assert (
        len(shown.arguments["_unparsed"]) < CUT_CALL_KEEP_CHARS + 40
        and "cut off: 30000 chars" in shown.arguments["_unparsed"]
    )
    assert shown.raw_arguments is None and out[1] is ok
    assert cut.tool_calls[0].arguments["_unparsed"] == "x" * 30_000, "the stored message keeps all of it"
    plain = [ok]
    assert shrink_cut_calls(plain) is plain


def test_superseded_todo_lists_are_elided_but_the_newest_stays():
    from k3code.context_budget import elide_old_results

    def todos(n: int) -> dict:
        return {"todos": [{"content": f"step {i} " + "d" * 40, "status": "pending"} for i in range(n)]}

    msgs: list[Message] = []
    for i in range(10):
        msgs += [_call_message(i, "todo", todos(10)), _result(i, "todo")]
    out = elide_old_results(msgs, over=True, elided=set(), window=None)
    args = [m.tool_calls[0].arguments["todos"] for m in out if m.role == "assistant"]
    assert all(isinstance(a, str) and a.startswith("[a list of 10 items") for a in args[:4]), args[:4]  # old steps
    assert isinstance(args[-1], list), "the newest list is the one the model works from"


@pytest.mark.parametrize(
    ("command", "elided"),
    [
        ("cat > app.py <<'EOF'\n" + "print(1)\n" * 60 + "EOF", True),
        ("cat <<EOF >> notes.md\n" + "line\n" * 80 + "EOF", True),
        ("tee out.txt <<'X'\n" + "line\n" * 80 + "X", True),
        ("python - <<'EOF'\n" + "print(1)\n" * 60 + "EOF", False),  # a script that is not on disk
        ("python - <<'EOF' 2>/dev/null\n" + "print(1)\n" * 60 + "EOF", False),
        ("cat > app.py <<'EOF'\nprint(1)\nEOF", False),  # small
    ],
)
def test_heredoc_text_a_bash_command_wrote_to_a_file_is_elided_from_old_calls(command, elided):
    from k3code.context_budget import elide_old_results

    msgs: list[Message] = []
    for i in range(9):
        msgs += [_call_message(i, "bash", {"command": command if i == 0 else f"echo {i}"}), _result(i, "bash")]
    out = elide_old_results(msgs, over=True, elided=set(), window=None)
    first = out[0].tool_calls[0].arguments["command"]
    assert first != command if elided else first == command
    if elided:
        assert first.splitlines()[0] == command.splitlines()[0] and "they are in the file now" in first


def test_at_most_six_of_the_newest_results_are_protected_whatever_the_window():
    from k3code.context_budget import ELIDE_KEEP_CALLS, kept_calls

    asst = Message(role="assistant", content=None)
    many = [asst, *(Message(role="tool", content="x", tool_call_id=f"t{i}", name="read") for i in range(30))]
    assert kept_calls(many, 10_000_000) == ELIDE_KEEP_CALLS == 6


def test_the_compaction_trigger_counts_elided_results_as_their_markers():
    """A 40-read turn of 38 KB files on a 100k window: the request is ~42k tokens, and the estimate said 408k, so the
    next turn compacted (a summary call, and the detail gone) for nothing."""
    msgs: list[dict] = []
    for i in range(40):
        msgs.append(
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"r{i}", "name": "read", "arguments": {}}]}
        )
        msgs.append({"role": "tool", "name": "read", "tool_call_id": f"r{i}", "content": "x" * 38_000})
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"w{i}", "name": "write", "arguments": {"path": "a", "content": "y" * 8_000}}],
            }
        )
        msgs.append({"role": "tool", "name": "write", "tool_call_id": f"w{i}", "content": "Wrote a"})
    whole = _estimate_tokens(msgs, 10_000)
    elided = {f"r{i}" for i in range(36)} | {f"args:w{i}" for i in range(36)}
    short = _estimate_tokens(msgs, 10_000, elided)
    assert short < whole // 3, (short, whole)
    # the four newest steps are still counted whole
    assert short > 4 * 2_500
