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
