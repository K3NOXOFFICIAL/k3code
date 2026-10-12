"""One-shot side calls (titles, summaries, classification, judges, advisor) count against the session's budgets."""

from __future__ import annotations

import pytest

from k3code.providers.types import Message
from k3code.reliability.governor import Budget
from k3code.routing.caller import NO_EFFORT_KINDS
from k3code.routing.tiers import TaskKind
from test_autonomy_gateway import call, make


async def test_a_side_call_is_charged_to_the_sessions_budget_and_the_day_ledger(tmp_path, monkeypatch):
    steps = [
        {"type": "text", "text": "a title"},
        {"type": "usage", "prompt_tokens": 500, "completion_tokens": 100},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="auto")
    await call(server, "session.create", {"cwd": str(tmp_path)})
    live = server.session
    server._ensure_router(None)
    rel = await server._reliability_for(live)
    assert rel.governor is not None
    rel.governor.add_budget(Budget(scope="session", tokens=1_000_000))
    rel.governor.day_ledger.roll(rel.governor.today())  # starts today's count from this test's own (empty) log
    before_day = rel.governor.day_ledger.tokens

    res = await server.model_caller.complete(
        TaskKind.TITLE, [Message(role="user", content="name this")], session_id=live.session_id
    )
    assert res.text == "a title"
    assert rel.governor._budgets["session"].used_tokens == 600
    assert rel.governor.day_ledger.tokens - before_day == 600


async def test_a_side_call_that_pushes_the_session_over_its_cap_stops_the_next_turn(tmp_path, monkeypatch):
    steps = [{"type": "text", "text": "ok"}, {"type": "usage", "prompt_tokens": 900, "completion_tokens": 200}]
    server = make(tmp_path, monkeypatch, steps, mode="auto")
    await call(server, "session.create", {"cwd": str(tmp_path)})
    live = server.session
    server._ensure_router(None)
    rel = await server._reliability_for(live)
    rel.governor.add_budget(Budget(scope="session", tokens=1000))
    await server.model_caller.complete(TaskKind.TITLE, [Message(role="user", content="x")], session_id=live.session_id)
    err = rel.check_budgets()
    assert err is not None and err.scope == "session"


async def test_a_call_with_no_session_is_not_charged(tmp_path, monkeypatch):
    steps = [{"type": "text", "text": "ok"}, {"type": "usage", "prompt_tokens": 50, "completion_tokens": 5}]
    server = make(tmp_path, monkeypatch, steps, mode="auto")
    await call(server, "session.create", {"cwd": str(tmp_path)})
    server._ensure_router(None)
    rel = await server._reliability_for(server.session)
    rel.governor.add_budget(Budget(scope="session", tokens=1_000_000))
    await server.model_caller.complete(TaskKind.TITLE, [Message(role="user", content="x")])
    assert rel.governor._budgets["session"].used_tokens == 0


async def test_a_side_call_never_carries_the_sessions_reasoning_effort_but_the_advisor_does(tmp_path, monkeypatch):
    from k3code.providers import effort as effort_mod

    seen: dict[str, str | None] = {}

    steps = [{"type": "text", "text": "ok"}]
    server = make(tmp_path, monkeypatch, steps, mode="auto")
    await call(server, "session.create", {"cwd": str(tmp_path)})

    original = server.model_caller._routers

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        async def complete(self, messages, tools, **kw):
            seen["effort"] = effort_mod.REASONING_EFFORT.get()
            return await self.inner.complete(messages, tools, **kw)

    class Routers:
        def get(self, tier):
            return Spy(original().get(tier))

    server.model_caller._routers = lambda: Routers()
    token = effort_mod.REASONING_EFFORT.set("max")
    try:
        await server.model_caller.complete(TaskKind.TITLE, [Message(role="user", content="x")])
        assert seen["effort"] is None
        await server.model_caller.complete(TaskKind.ADVISOR, [Message(role="user", content="x")])
        assert seen["effort"] == "max"
        assert effort_mod.REASONING_EFFORT.get() == "max", "restored after the side call"
    finally:
        effort_mod.REASONING_EFFORT.reset(token)


async def test_a_side_call_before_the_sessions_first_turn_is_charged_too(tmp_path, monkeypatch):
    """A resumed session's first compaction (or a first-turn classifier) runs before any turn built the bundle."""
    import time

    from k3code.reliability.governor import DAY_LEDGER

    steps = [{"type": "text", "text": "a title"}, {"type": "usage", "prompt_tokens": 500, "completion_tokens": 100}]
    server = make(tmp_path, monkeypatch, steps, mode="auto")
    await call(server, "session.create", {"cwd": str(tmp_path)})
    live = server.session
    server._ensure_router(None)
    assert live.reliability is None, "no turn has run"
    DAY_LEDGER.roll(time.strftime("%Y-%m-%d"))
    before = DAY_LEDGER.tokens
    await server.model_caller.complete(TaskKind.TITLE, [Message(role="user", content="x")], session_id=live.session_id)
    assert live.reliability is not None
    assert DAY_LEDGER.tokens - before == 600
    await server.close()


@pytest.mark.parametrize("kind", sorted(NO_EFFORT_KINDS, key=lambda k: k.value))
async def test_every_side_call_kind_that_should_think_little_drops_the_sessions_effort(tmp_path, monkeypatch, kind):
    from k3code.providers import effort as effort_mod

    seen: dict[str, str | None] = {}
    server = make(tmp_path, monkeypatch, [{"type": "text", "text": "ok"}], mode="auto")
    await call(server, "session.create", {"cwd": str(tmp_path)})
    original = server.model_caller._routers

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        async def complete(self, messages, tools, **kw):
            seen["effort"] = effort_mod.REASONING_EFFORT.get()
            return await self.inner.complete(messages, tools, **kw)

    class Routers:
        def get(self, tier):
            return Spy(original().get(tier))

    server.model_caller._routers = lambda: Routers()
    token = effort_mod.REASONING_EFFORT.set("max")
    try:
        await server.model_caller.complete(kind, [Message(role="user", content="x")])
        assert seen["effort"] is None, kind
    finally:
        effort_mod.REASONING_EFFORT.reset(token)
    await server.close()


async def test_the_first_side_call_of_a_cold_daemon_is_counted_once_by_the_day_ledger(tmp_path, monkeypatch):
    """Charge, then log: the ledger reads the log on first use, so a row written first would be counted twice."""
    from k3code.reliability.governor import DAY_LEDGER

    steps = [{"type": "text", "text": "a title"}, {"type": "usage", "prompt_tokens": 500, "completion_tokens": 100}]
    server = make(tmp_path, monkeypatch, steps, mode="auto")  # the server starts with a cold ledger
    await call(server, "session.create", {"cwd": str(tmp_path)})
    live = server.session
    server._ensure_router(None)
    await server.model_caller.complete(TaskKind.TITLE, [Message(role="user", content="x")], session_id=live.session_id)
    assert DAY_LEDGER.tokens == 600
    await server.close()


async def test_an_advisor_call_that_thought_its_whole_cap_away_is_asked_again_without_the_effort():
    from types import SimpleNamespace

    from k3code.providers import effort as effort_mod
    from k3code.routing.caller import ModelCaller

    seen: list[str | None] = []

    class Router:
        async def complete(self, messages, tools, **kw):
            seen.append(effort_mod.REASONING_EFFORT.get())
            text = "" if len(seen) == 1 else "VERDICT: pass"
            return SimpleNamespace(content=text, usage=None)

    class Routers:
        def get(self, tier):
            return Router()

    caller = ModelCaller(lambda: Routers(), SimpleNamespace(task_tiers=None), None)
    token = effort_mod.REASONING_EFFORT.set("max")
    try:
        res = await caller.complete(TaskKind.ADVISOR, [Message(role="user", content="x")], max_tokens=500)
        assert res.text == "VERDICT: pass" and seen == ["max", None]
        assert effort_mod.REASONING_EFFORT.get() == "max", "restored after the retry"
        seen.clear()
        res = await caller.complete(TaskKind.TITLE, [Message(role="user", content="x")])  # no effort to drop: one call
        assert len(seen) == 1 and res.text == ""
    finally:
        effort_mod.REASONING_EFFORT.reset(token)
