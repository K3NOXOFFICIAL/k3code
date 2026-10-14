"""A goal that makes no progress is slowed, not left to run at full speed: prose without a tool call, a loop-guard stop
retried again and again, a backoff that outlives the prompt cache; and a spend-cap stop says what it is."""

from __future__ import annotations

import asyncio

import pytest

import k3code.goals as goals
from k3code.goals import MAX_LOOP_RECOVERIES, GoalManager
from test_goal_persist import judge_of, manager

CONTINUE = ("continue", "more to do", False, False)


async def test_prose_turns_without_a_tool_call_back_off_after_five_in_a_row():
    mgr, slept, _ = manager()
    notices: list[str] = []
    mgr.on_wait = notices.append
    mgr.set("ship it")
    judge = judge_of(CONTINUE)
    for n in range(1, 9):  # every reply differs, so only the length of the streak counts
        await mgr.evaluate_after_turn(f"Here is my plan, version {n}: think about {'x' * n}.", judge, used_tools=False)
    assert slept == [15.0, 30.0, 60.0, 120.0]  # none for the first four turns, then the usual ramp
    assert len(notices) == 4 and "no tool" in notices[0] and "/goal clear stops the goal" in notices[0]
    assert mgr.state.status == "active"


async def test_a_turn_that_calls_a_tool_starts_the_count_again():
    mgr, slept, _ = manager()
    mgr.set("ship it")
    judge = judge_of(CONTINUE)
    for n in range(4):
        await mgr.evaluate_after_turn(f"thinking {'y' * n}", judge, used_tools=False)
    await mgr.evaluate_after_turn("ran the tests, they pass", judge, used_tools=True)
    for n in range(4):  # four more prose turns: still under the limit because the tool turn reset the count
        await mgr.evaluate_after_turn(f"more thinking {'z' * n}", judge, used_tools=False)
    assert slept == [] and mgr.state.consecutive_toolless == 4


async def test_a_goal_whose_turns_call_tools_is_never_slowed():
    mgr, slept, _ = manager()
    mgr.set("ship it")
    judge = judge_of(CONTINUE)
    for _ in range(40):
        await mgr.evaluate_after_turn("same summary every turn", judge, used_tools=True)
    assert slept == []


async def test_the_same_reply_three_times_without_a_tool_call_backs_off_sooner():
    mgr, slept, _ = manager()
    mgr.set("ship it")
    judge = judge_of(CONTINUE)
    await mgr.evaluate_after_turn("I will look into it (step 1).", judge, used_tools=False)
    await mgr.evaluate_after_turn("I WILL look into it (step 2).", judge, used_tools=False)  # case and digits aside
    assert slept == []
    await mgr.evaluate_after_turn("I will  look into it (step 3).", judge, used_tools=False)
    assert slept == [15.0]
    await mgr.evaluate_after_turn("a different answer entirely", judge, used_tools=False)
    assert slept == [15.0]  # the run of identical replies ended; four turns without a tool are still under five


async def test_a_turn_budget_still_pauses_instead_of_waiting_when_the_streak_is_long():
    mgr, slept, _ = manager()
    mgr.set("ship it", max_turns=8)
    judge = judge_of(CONTINUE)
    for n in range(8):
        decision = await mgr.evaluate_after_turn(f"prose {'q' * n}", judge, used_tools=False)
    assert (
        not decision.should_continue and mgr.state.status == "paused" and len(slept) == 3
    )  # turns 5, 6 and 7 wait; the 8th pauses


def test_the_backoff_cap_stays_under_the_five_minute_prompt_cache():
    assert goals.PERSIST_BACKOFF_MAX_S == 240.0
    assert [GoalManager.backoff(n) for n in range(7)] == [15.0, 30.0, 60.0, 120.0, 240.0, 240.0, 240.0]


# ── the gateway ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def server_live(tmp_path, monkeypatch):
    async def make():
        from m1cmd_helpers import git_repo, make_server, new_session

        repo = git_repo(tmp_path / "repo")
        server, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy={"advisor_on_goal": False})
        sid = await new_session(server, repo)
        live = server.live_for(server.store.get(sid))
        toasts: list[str] = []
        server.notify_blocker = lambda sid, text, **kw: toasts.append(text)  # type: ignore[method-assign]
        return server, live, server.goal_manager(live), toasts

    return make


@pytest.fixture
def waits(monkeypatch):
    """Record the n of every backoff and wait for nothing."""
    seen: list[int] = []
    monkeypatch.setattr(GoalManager, "backoff", staticmethod(lambda n: seen.append(n) or 0.0))
    return seen


async def test_the_gateway_tells_the_manager_whether_the_turn_called_a_tool(server_live, waits):
    server, live, mgr, _ = await server_live()
    mgr.set("ship it")
    server.goal_judge = judge_of(
        CONTINUE, CONTINUE, CONTINUE, CONTINUE, CONTINUE, CONTINUE, ("done", "ok", False, False)
    )
    calls = iter([0, 0, 0, 0, 0, 0, 0])
    texts = iter(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"])

    async def scripted_turn(session, prompt, **kw):
        session.last_tool_calls = next(calls)
        return "done", next(texts)

    server._run_one_turn = scripted_turn  # type: ignore[method-assign]
    await asyncio.wait_for(server._run_turn_locked(live, "start"), 20)
    assert waits == [0, 1]  # turns 5 and 6 of seven waited (the 7th was judged done)
    await server.close()


async def test_a_real_turn_without_tool_calls_records_zero_tool_calls(server_live):
    server, live, _, _ = await server_live()
    status, _text = await asyncio.wait_for(server._run_one_turn(live, "hello"), 20)
    assert status == "done" and live.last_tool_calls == 0 and live.last_api_calls == 1
    await server.close()


LOOP = 'tool:bash:{"command": "pytest -x"}'


def _loop_turns(outcomes: list[str], prompts: list[str]):
    it = iter(outcomes)

    async def scripted_turn(session, prompt, **kw):
        prompts.append(prompt)
        status = next(it)
        session.last_error = ""
        session.needs_input = status == "loop"
        session.loop_repeat = LOOP if status == "loop" else ""
        return ("needs_input" if status == "loop" else "done"), ("shipped" if status == "done" else "")

    return scripted_turn


async def test_a_goal_whose_model_keeps_repeating_one_call_is_paused_after_five_retries(server_live, waits):
    server, live, mgr, toasts = await server_live()
    mgr.set("ship it")
    server.goal_judge = judge_of(("done", "finished", False, False))
    prompts: list[str] = []
    server._run_one_turn = _loop_turns(["loop"] * 12, prompts)  # type: ignore[method-assign]
    await asyncio.wait_for(server._run_turn_locked(live, "start"), 20)
    assert len(prompts) == 1 + MAX_LOOP_RECOVERIES  # the user's prompt and five retries, not a twelfth attempt
    assert "`bash`" in prompts[1] and "pytest -x" in prompts[1] and "do not repeat it" in prompts[1]
    assert mgr.state.status == "paused" and mgr.state.paused_reason == "needs_input"
    assert len(toasts) == 1 and "kept repeating the call `bash`" in toasts[0] and "/goal resume" in toasts[0]
    assert live.goal_loop_streak == 0  # /goal resume gets a fresh set of retries
    await server.close()


async def test_a_single_loop_guard_stop_is_still_retried_and_a_good_turn_clears_the_count(server_live, waits):
    server, live, mgr, toasts = await server_live()
    mgr.set("ship it")
    server.goal_judge = judge_of(("continue", "more", False, False), ("done", "finished", False, False))
    prompts: list[str] = []
    # 4 stops, a good turn, 4 stops, a good turn that ends the goal: never more than five stops in a row
    plan = ["loop"] * 4 + ["done"] + ["loop"] * 4 + ["done"]
    server._run_one_turn = _loop_turns(plan, prompts)  # type: ignore[method-assign]
    await asyncio.wait_for(server._run_turn_locked(live, "start"), 20)
    assert len(prompts) == len(plan) and mgr.state.status == "done" and toasts == []
    await server.close()


async def test_a_stop_for_a_question_is_not_capped_like_a_loop(server_live, waits):
    server, live, mgr, toasts = await server_live()
    mgr.set("ship it")
    server.goal_judge = judge_of(("done", "finished", False, False))
    outcomes = iter(["ask"] * 9 + ["done"])

    async def scripted_turn(session, prompt, **kw):
        status = next(outcomes)
        session.needs_input = status == "ask"
        return ("needs_input", "") if status == "ask" else ("done", "shipped")

    server._run_one_turn = scripted_turn  # type: ignore[method-assign]
    await asyncio.wait_for(server._run_turn_locked(live, "start"), 20)
    assert mgr.state.status == "done" and toasts == []
    await server.close()


async def _stopped_toast(server_live, *, error: str, budget: bool) -> str:  # a persistent goal retries the rest
    server, live, mgr, toasts = await server_live()
    mgr.set("ship it")

    async def stopped(session, prompt, **kw):
        session.needs_input, session.budget_stopped, session.last_error = True, budget, error
        return "needs_input", ""

    server._run_one_turn = stopped  # type: ignore[method-assign]
    await asyncio.wait_for(server._run_turn_locked(live, "start"), 20)
    assert mgr.state.status == "paused" and len(toasts) == 1
    await server.close()
    return toasts[0]


async def test_a_spend_cap_stop_names_the_cap_instead_of_asking_for_input(server_live):
    text = await _stopped_toast(server_live, error="day budget exceeded: 5.02 of 5.00 EUR", budget=True)
    assert "spend cap" in text and "5.02 of 5.00 EUR" in text and "reliability" in text and "resets" in text
    assert "needs your input" not in text and "Answer" not in text


async def test_the_max_turns_cap_stop_names_the_cap_too(server_live):
    text = await _stopped_toast(server_live, error="", budget=True)
    assert "max_turns" in text and "needs your input" not in text


async def test_a_real_question_still_asks_for_an_answer(server_live):
    server, live, mgr, toasts = await server_live()
    mgr.set("ship it")
    server._block_goal_for(live, "needs_input")
    assert len(toasts) == 1 and "needs your input" in toasts[0] and "Answer, then /goal resume" in toasts[0]
    await server.close()
