"""The decision model (context.decision_model): a small model picks which old tool results the main model still needs
before a batch of them is elided; the others are dropped, with a short note of what still matters."""

from __future__ import annotations

import pytest

from k3code.context_select import build_prompt, decision_settings, parse_decision
from k3code.providers.types import Message
from k3code.reliability import Reliability
from k3code.routing.tiers import TaskKind, Tier
from test_auto_compaction import Recording, seed
from test_token_context_budget import make_loop, reads_of
from test_wire_clip import Recorder, text_reply, tool_reply

ELIDED = "[earlier read result elided: "


async def run_with(tmp_path, decide, *, n: int = 9, window: int = 16_000):
    calls = reads_of(tmp_path, n)
    provider = Recorder([*(tool_reply(c) for c in calls), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=window)
    loop.max_turns = 0
    loop.decide_context = decide
    async for _ in loop.run("read them all"):
        pass
    return loop, provider


async def test_the_decision_model_keeps_some_results_and_leaves_notes_for_others(tmp_path):
    asked: list[list[str]] = []

    async def decide(wire, candidates):
        asked.append([m.tool_call_id for m in candidates])
        return {"r0": "the config lives at line 3", "r1": "keep"}

    _, provider = await run_with(tmp_path, decide)
    assert asked and asked[0][:2] == ["r0", "r1"]  # it was asked about the old results before they were elided
    sent = {m.tool_call_id: m.content for m in provider.requests[-1][0] if m.role == "tool"}
    assert sent["r0"].startswith(ELIDED) and sent["r0"].endswith("\nStill relevant: the config lives at line 3")
    assert "file 1" in sent["r1"] and len(sent["r1"]) > 3000  # kept whole
    dropped = [k for k, v in sent.items() if k not in ("r0", "r1") and v.startswith(ELIDED)]
    assert dropped and not any("Still relevant" in sent[k] for k in dropped)  # not named: dropped, no note


async def test_a_failing_decision_model_falls_back_to_plain_elision(tmp_path):
    async def decide(wire, candidates):
        raise TimeoutError("too slow")

    _, provider = await run_with(tmp_path, decide)
    _, plain = await run_with(tmp_path, None)
    assert [r[0] for r in provider.requests] == [r[0] for r in plain.requests]  # exactly what plain elision sends
    assert any(m.content.startswith(ELIDED) for m in provider.requests[-1][0] if m.role == "tool")


async def test_what_it_keeps_must_fit_under_the_threshold(tmp_path):
    """Keeping everything would leave the request over the threshold, so the next step would ask again and rewrite the
    prefix again; the oldest kept results are elided after all."""

    async def decide(wire, candidates):
        return {m.tool_call_id: "keep" for m in candidates}

    _, provider = await run_with(tmp_path, decide, n=20, window=16_000)
    sent = {m.tool_call_id: m.content for m in provider.requests[-1][0] if m.role == "tool"}
    assert sent["r0"].startswith(ELIDED)
    assert all(f"file {i}" in sent[f"r{i}"] for i in range(14, 20))  # the last six stay whole


async def test_notes_stay_put_so_the_request_prefix_rarely_changes(tmp_path):
    async def decide(wire, candidates):
        return {m.tool_call_id: f"note for {m.tool_call_id}" for m in candidates}

    _, provider = await run_with(tmp_path, decide, n=40, window=30_000)
    sent = [[m.content for m in request[0]] for request in provider.requests]
    rewrites = sum(1 for before, after in zip(sent, sent[1:], strict=False) if after[: len(before)] != before)
    assert 1 <= rewrites <= 5  # one per batch, as with plain elision
    last = {m.tool_call_id: m.content for m in provider.requests[-1][0] if m.role == "tool"}
    assert last["r0"].endswith("Still relevant: note for r0")


def test_the_decision_is_parsed_from_the_models_json():
    cands = [Message(role="tool", content="x" * 3000, tool_call_id=f"c{i}", name="read") for i in range(3)]
    text = 'Sure:\n```json\n{"keep": [2], "notes": {"1": "  main()  at\\nline 40 ", "3": "", "9": "ghost"}}\n```'
    assert parse_decision(text, cands) == {"c0": "main() at line 40", "c1": "keep"}
    with pytest.raises(ValueError):
        parse_decision("no idea", cands)


def test_the_prompt_numbers_the_candidates_and_shortens_them():
    from k3code.providers.types import ToolCall

    call = ToolCall(id="c0", name="read", arguments={"path": "a.py"})
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="fix the bug in a.py"),
        Message(role="assistant", content=None, tool_calls=[call]),
        Message(role="tool", content="y" * 9000, tool_call_id="c0", name="read"),
    ]
    prompt = build_prompt(messages, [messages[3]], 40_000)
    assert "fix the bug in a.py" in prompt
    assert '### [1] read {"path": "a.py"} (9000 chars)' in prompt
    assert len(prompt) < 3000


def test_settings_are_off_by_default():
    class Cfg:
        context: dict = {}

    assert not decision_settings(Cfg()).enabled
    Cfg.context = {"decision_model": True}
    assert decision_settings(Cfg()).enabled
    Cfg.context = {"decision_model": {"enabled": True, "model": "tiny", "at_ratio": 0.3}}
    s = decision_settings(Cfg())
    assert (s.enabled, s.model, s.at_ratio) == (True, "tiny", 0.3)


async def test_the_gateway_sends_decisions_to_the_named_model_or_the_cheap_tier(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={})
    rel = Reliability.from_settings(None, session="d", home=tmp_path / "home")
    assert server._build_loop(live, rel, server.router, TaskKind.INTERACTIVE_TURN, None).decide_context is None

    seen = []

    async def complete(kind, messages, **kw):
        seen.append((kind, kw.get("router")))

        class Res:
            text = '{"keep": [1]}'

        return Res()

    monkeypatch.setattr(server.model_caller, "complete", complete)
    cand = [Message(role="tool", content="z" * 3000, tool_call_id="c0", name="read")]
    server.config.context = {"decision_model": {"enabled": True, "at_ratio": 0.3}}
    loop = server._build_loop(live, rel, server.router, TaskKind.INTERACTIVE_TURN, None)
    assert loop.elide_at_ratio == 0.3
    assert await loop.decide_context([], cand) == {"c0": "keep"}
    assert seen[-1] == (TaskKind.CONTEXT_SELECT, None)  # the tier policy decides: cheap by default

    server.providers = [Recording()]
    server.config.context = {"decision_model": {"enabled": True, "model": "tiny-model"}}
    loop = server._build_loop(live, rel, server.router, TaskKind.INTERACTIVE_TURN, None)
    await loop.decide_context([], cand)
    assert [e.model for e in seen[-1][1].chain] == ["tiny-model"]
    await server.close()


def test_context_select_runs_on_the_cheap_tier_by_default():
    from k3code.routing.tiers import tier_for

    assert tier_for(TaskKind.CONTEXT_SELECT) is Tier.CHEAP
    assert tier_for(TaskKind.CONTEXT_SELECT, {"context_select": "fast"}) is Tier.FAST
