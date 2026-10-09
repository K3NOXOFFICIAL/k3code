"""History weight: per-model context windows drive compaction, old tool results are elided from big requests (never
from stored history), and a re-read of an unchanged file in the same turn is answered with a pointer."""

from __future__ import annotations

from types import SimpleNamespace

from k3code.agent.loop import AgentLoop
from k3code.context_budget import compact_threshold, context_window
from k3code.gateway.server import _estimate_tokens
from k3code.providers.types import ToolCall
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
    assert context_window(cfg, "llama-3-8b") == 32_000
    assert compact_threshold(cfg, "claude-x") == 140_000  # 0.7 of the window
    cfg.context = {"compact_at_ratio": 0.5}
    assert compact_threshold(cfg, "gpt-4o") == 64_000
    cfg.context = {"compact_at_tokens": 5000, "compact_at_ratio": 0.5}
    assert compact_threshold(cfg, "gpt-4o") == 5000  # the absolute value overrides


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


def reads_of(tmp_path, n: int) -> list[ToolCall]:
    calls = []
    for i in range(n):
        f = tmp_path / f"f{i}.txt"
        f.write_text(f"file {i}\n" + ("q" * 59 + "\n") * 50)  # ~3 kB
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
    for i in range(3):  # older than the last 6 tool calls
        assert sent[f"r{i}"].startswith("[earlier read result elided: ")
        assert "re-run if needed]" in sent[f"r{i}"]
    for i in range(3, 9):
        assert f"file {i}" in sent[f"r{i}"] and len(sent[f"r{i}"]) > 3000
    stored = [m.content for m in loop.turn_messages if m.role == "tool"]
    assert all(len(c) > 3000 for c in stored)  # the conversation itself keeps every result
    transcript = [m.content for m in loop.reliability.load_transcript() if m.role == "tool"]
    assert all(len(c) > 3000 for c in transcript)


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
