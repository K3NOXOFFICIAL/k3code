"""One-shot side calls (titles, summaries, classification, judges, advisor) count against the session's budgets."""

from __future__ import annotations

from k3code.providers.types import Message
from k3code.reliability.governor import Budget
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
