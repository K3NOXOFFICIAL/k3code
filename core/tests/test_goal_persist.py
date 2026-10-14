"""A goal keeps going until it is done: what used to pause it makes it wait, retry or try another way. Only what the
user set (a turn budget, a check retry limit, a spend budget, /stop, /goal pause|clear, /daemon pause) stops it."""

from __future__ import annotations

import asyncio

import pytest

import k3code.goals as goals
from k3code.goals import (
    BLOCKED_NOTICE_EVERY,
    CONTINUATION_PROMPT_BLOCKED_TEMPLATE,
    GATE_COULD_NOT_RUN,
    GoalManager,
    GoalState,
)


def manager(**kw) -> tuple[GoalManager, list[float], dict]:
    box: dict = {"goal": None}
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    mgr = GoalManager(lambda: box["goal"], lambda s: box.__setitem__("goal", s), persist=True, sleep=sleep, **kw)
    return mgr, slept, box


def judge_of(*answers):
    """A judge that answers in turn (the last one repeats): (verdict, reason, parse_failed, transport_failed)."""
    calls = []

    async def judge(goal, response):
        calls.append(response)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


async def test_a_blocked_verdict_sends_the_agent_to_find_another_way_it_does_not_pause():
    mgr, _, _ = manager()
    mgr.set("ship it")
    judge = judge_of(("blocked", "needs the API key", False, False))
    for n in range(1, 12):
        decision = await mgr.evaluate_after_turn("I cannot continue", judge)
        assert decision.should_continue and decision.verdict == "blocked" and mgr.state.status == "active"
        assert "another way" in decision.prompt and "needs the API key" in decision.prompt
        assert "Do not stop to ask the user" in decision.prompt
        assert ("/goal clear stops it" in decision.message) == (n % BLOCKED_NOTICE_EVERY == 0)
    assert mgr.state.consecutive_blocked == 11


async def test_a_judge_that_cannot_be_reached_is_waited_for_not_given_up_on():
    mgr, slept, _ = manager()
    mgr.set("ship it")
    judge = judge_of(
        ("continue", "judge error: ConnectError", False, True),
        ("continue", "judge error: ConnectError", False, True),
        ("continue", "judge error: ConnectError", False, True),
        ("done", "all good", False, False),
    )
    decision = await mgr.evaluate_after_turn("done!", judge)
    assert decision.verdict == "done" and mgr.state.status == "done"
    assert slept == [15.0, 30.0, 60.0] and len(judge.calls) == 4  # one turn, one answer asked for four times
    assert mgr.state.turns_used == 1


async def test_the_wait_for_a_judge_ends_when_the_user_clears_the_goal():
    mgr, _, _ = manager()
    mgr.set("ship it")
    judge = judge_of(("continue", "judge error", False, True))
    real_sleep = mgr._sleep

    async def clear_while_waiting(seconds):
        await real_sleep(seconds)
        mgr.clear()

    mgr._sleep = clear_while_waiting
    decision = await mgr.evaluate_after_turn("done!", judge)
    assert not decision.should_continue and mgr.state is None  # not resurrected by the retry loop


async def test_empty_replies_and_judge_noise_never_pause_a_persistent_goal_but_slow_it_down():
    mgr, slept, _ = manager()
    mgr.set("ship it")
    noisy = judge_of(("continue", "judge reply was not JSON", True, False))
    for _ in range(25):
        decision = await mgr.evaluate_after_turn("   ", noisy)
        assert decision.should_continue and mgr.state.status == "active"
    assert "ended without any text" in decision.message and "concrete step" in decision.prompt
    assert mgr.state.consecutive_parse_failures == 25 and mgr.state.consecutive_empty_replies == 25
    # the first two empty turns go at full speed; then the pause before each turn doubles up to four minutes
    assert slept[:4] == [15.0, 30.0, 60.0, 120.0] and max(slept) == 240.0 and len(slept) == 23


async def test_a_judge_that_is_not_readable_slows_the_goal_without_stopping_it():
    mgr, slept, _ = manager()
    mgr.set("ship it")
    garbage = judge_of(("continue", "judge reply was not JSON", True, False))
    for _ in range(4):
        await mgr.evaluate_after_turn("working on it", garbage)
    assert slept == []  # the first four turns go at full speed
    for n in range(5, 9):
        decision = await mgr.evaluate_after_turn("working on it", garbage)
        assert decision.should_continue and f"{n} turns" in decision.message
    assert slept == [15.0, 30.0, 60.0, 120.0] and mgr.state.status == "active"


async def test_a_turn_that_failed_still_counts_against_an_explicit_turn_budget():
    mgr, _, _ = manager()
    mgr.set("ship it", max_turns=2)
    assert mgr.count_recovery_turn() is True
    assert mgr.count_recovery_turn() is False  # that was the last of the two
    unlimited, _, _ = manager()
    unlimited.set("ship it")
    assert all(unlimited.count_recovery_turn() for _ in range(500))


async def test_a_check_that_cannot_run_is_run_again_after_a_pause_without_spending_a_turn(monkeypatch):
    results = iter(
        [(False, GATE_COULD_NOT_RUN, "[no sandbox]"), (False, GATE_COULD_NOT_RUN, "[no sandbox]"), (True, 0, "ok")]
    )

    async def gate(g, *, cwd=None):
        return next(results)

    monkeypatch.setattr(goals, "run_gate", gate)
    mgr, slept, _ = manager()
    waits: list[str] = []
    mgr.on_wait = waits.append
    mgr.set("ship it", check="make test")
    decision = await mgr.evaluate_after_turn("done!", judge_of(("done", "finished", False, False)))
    assert decision.verdict == "done" and mgr.state.status == "done"
    assert slept == [15.0, 30.0] and len(waits) == 2 and "cannot run" in waits[0]
    assert mgr.state.turns_used == 1


async def test_a_failing_check_goes_on_forever_unless_a_limit_was_set(monkeypatch):
    async def gate(g, *, cwd=None):
        return False, 1, "still red"

    monkeypatch.setattr(goals, "run_gate", gate)
    mgr, _, _ = manager()
    mgr.set("ship it", check="make test")
    for _ in range(80):
        decision = await mgr.evaluate_after_turn("done!", judge_of(("done", "finished", False, False)))
        assert decision.should_continue and "make sure" not in (decision.prompt or "").lower()
    assert "keep working toward what it is meant to verify" in decision.prompt
    limited, _, _ = manager(default_gate_max_retries=3)
    limited.set("ship it", check="make test")
    outcomes = [await limited.evaluate_after_turn("done!", judge_of(("done", "x", False, False))) for _ in range(4)]
    assert [o.should_continue for o in outcomes] == [True, True, True, False] and limited.state.status == "paused"


async def test_a_turn_budget_the_user_set_still_pauses_a_persistent_goal():
    mgr, _, _ = manager()
    mgr.set("ship it", max_turns=2)
    judge = judge_of(("blocked", "stuck", False, False))
    first = await mgr.evaluate_after_turn("x", judge)
    second = await mgr.evaluate_after_turn("x", judge)
    assert first.should_continue and not second.should_continue
    assert mgr.state.paused_reason == "turn budget exhausted (2/2)"


def test_a_stored_300_turn_budget_is_the_old_default_but_turns_300_is_a_choice():
    legacy = GoalState.from_dict({"goal": "old goal", "max_turns": 300, "turns_used": 120})
    assert legacy.max_turns == 0 and not legacy.budget_spent()
    stored = GoalState.from_dict({"goal": "g", "max_turns": 40})  # another number was chosen by someone
    assert stored.max_turns == 40
    mgr, _, box = manager()
    mgr.set("chosen", max_turns=300)
    assert box["goal"]["max_turns"] == 300 and GoalState.from_dict(box["goal"]).max_turns == 300
    mgr.set("default")
    assert GoalState.from_dict(box["goal"]).max_turns == 0


def test_the_prompts_of_a_persistent_goal_do_not_tell_the_model_to_stop_and_ask():
    mgr, _, _ = manager()
    mgr.set("ship it")
    for prompt in (mgr.kick_prompt(), mgr.continuation_prompt()):
        assert "Do not stop to ask the user" in prompt and "say so clearly and stop" not in prompt
    plain = GoalManager(*(lambda: mgr._load(), mgr._save_raw), persist=False)
    assert "say so clearly and stop" in plain.continuation_prompt()
    assert "Do not give up" in CONTINUATION_PROMPT_BLOCKED_TEMPLATE


async def test_without_persist_the_older_pausing_stays():
    box: dict = {"goal": None}
    mgr = GoalManager(lambda: box["goal"], lambda s: box.__setitem__("goal", s), persist=False)
    mgr.set("ship it")
    decision = await mgr.evaluate_after_turn("stuck", judge_of(("blocked", "needs a key", False, False)))
    assert not decision.should_continue and mgr.state.status == "paused"
    mgr.resume()
    for _ in range(goals.MAX_CONSECUTIVE_PARSE_FAILURES):
        decision = await mgr.evaluate_after_turn("x", judge_of(("continue", "garbage", True, False)))
    assert not decision.should_continue and "judge" in mgr.state.paused_reason


def test_backoff_doubles_up_to_four_minutes():
    assert [GoalManager.backoff(n) for n in range(7)] == [15.0, 30.0, 60.0, 120.0, 240.0, 240.0, 240.0]
    assert GoalManager.backoff(10_000) == 240.0


# ── the gateway: a turn that failed or stopped for input is retried ─────────────────────────────────────────────


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(GoalManager, "backoff", staticmethod(lambda n: 0.0))


async def test_a_failed_goal_turn_is_retried_until_it_finishes(tmp_path, monkeypatch, fast_retries):
    from m1cmd_helpers import git_repo, make_server, new_session

    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy={"advisor_on_goal": False})
    sid = await new_session(server, repo)
    live = server.live_for(server.store.get(sid))
    mgr = server.goal_manager(live)
    mgr.set("ship it")
    server.goal_judge = judge_of(("done", "finished", False, False))
    outcomes = iter([("error", "boom"), ("needs_input", ""), ("done", "it is shipped")])
    prompts: list[str] = []

    async def scripted_turn(session, prompt, **kw):
        prompts.append(prompt)
        status, text = next(outcomes)
        session.last_error = "provider fell over" if status == "error" else ""
        session.needs_input = status == "needs_input"
        return status, text

    server._run_one_turn = scripted_turn  # type: ignore[method-assign]
    status, text = await asyncio.wait_for(server._run_turn_locked(live, "start"), 20)
    assert (status, text) == ("done", "it is shipped") and len(prompts) == 3
    assert "did not finish: it failed (provider fell over)" in prompts[1]
    assert "stopped for input" in prompts[2] and "Do not stop to ask the user" in prompts[2]
    assert mgr.state.status == "done" and live.goal_retry_streak == 0
    await server.close()


async def test_a_spend_budget_stop_a_halt_and_a_user_stop_still_pause_the_goal(tmp_path, monkeypatch, fast_retries):
    from m1cmd_helpers import git_repo, make_server, new_session

    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy={"advisor_on_goal": False})
    sid = await new_session(server, repo)
    live = server.live_for(server.store.get(sid))
    mgr = server.goal_manager(live)
    mgr.set("ship it")

    async def over_budget(session, prompt, **kw):
        session.needs_input = session.budget_stopped = True
        return "needs_input", ""

    server._run_one_turn = over_budget  # type: ignore[method-assign]
    await asyncio.wait_for(server._run_turn_locked(live, "start"), 10)
    assert mgr.state.status == "paused" and mgr.state.paused_reason == "needs_input"
    mgr.resume()
    assert (
        server._goal_recovery(live, mgr, "interrupted") is None and server._goal_recovery(live, mgr, "halted") is None
    )
    live.budget_stopped = False
    assert server._goal_recovery(live, mgr, "error") is not None
    await server.close()


async def test_a_goal_stuck_on_blocked_waits_longer_each_turn_instead_of_retrying_at_full_speed():
    mgr, slept, _ = manager()
    mgr.set("ship it")
    judge = judge_of(("blocked", "needs the API key", False, False))
    for _ in range(BLOCKED_NOTICE_EVERY + 4):
        await mgr.evaluate_after_turn("I cannot continue", judge)
    assert slept == [15.0, 30.0, 60.0, 120.0]  # none for the first five, then the judge-failure ramp
    await mgr.evaluate_after_turn("did it", judge_of(("done", "ok", False, False)))
    assert mgr.state.consecutive_blocked == 0


def test_a_long_goal_is_quoted_by_its_start_and_a_pointer_and_a_short_one_whole():
    short = "ship the release"
    assert goals.quote(short) == short
    exact = "x" * goals.GOAL_QUOTE_CHARS
    assert goals.quote(exact) == exact
    long = "Implement the parser. " + "details " * 400
    quoted = goals.quote(long)
    assert quoted.startswith("Implement the parser.") and quoted.endswith(goals.GOAL_QUOTE_STUB)
    assert len(quoted) < len(long) // 4


async def test_continuation_prompts_quote_a_long_goal_but_the_kick_and_the_judge_get_it_whole():
    mgr, _, _ = manager()
    long = ("Migrate everything. " + "part " * 300).strip()
    mgr.set(long)
    assert long in mgr.kick_prompt()
    assert long not in mgr.continuation_prompt() and goals.quote(long) in mgr.continuation_prompt()
    judge = judge_of(("continue", "more to do", False, False))
    decision = await mgr.evaluate_after_turn("step", judge)
    assert long not in decision.prompt and goals.quote(long) in decision.prompt


async def test_a_failed_retry_does_not_leave_its_unanswered_prompt_in_the_history(tmp_path, monkeypatch, fast_retries):
    from m1cmd_helpers import git_repo, make_server, new_session

    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"], autonomy={"advisor_on_goal": False})
    sid = await new_session(server, repo)
    live = server.live_for(server.store.get(sid))
    mgr = server.goal_manager(live)
    mgr.set("ship it")
    server.goal_judge = judge_of(("done", "finished", False, False))
    outcomes = iter(["error", "error", "error", "done"])

    async def scripted_turn(session, prompt, **kw):
        status = next(outcomes)
        session.stored.messages = [*session.stored.messages, {"role": "user", "content": prompt}]  # persisted at once
        session.last_error = "provider fell over" if status == "error" else ""
        if status == "done":
            session.stored.messages = [*session.stored.messages, {"role": "assistant", "content": "shipped"}]
        return status, "shipped" if status == "done" else ""

    server._run_one_turn = scripted_turn  # type: ignore[method-assign]
    await asyncio.wait_for(server._run_turn_locked(live, "start"), 20)
    users = [m["content"] for m in live.stored.messages if m["role"] == "user"]
    # the user's own prompt, then only the retry that got an answer: not one recovery prompt per failed attempt
    assert users[0] == "start" and len(users) == 2 and "did not finish" in users[1]
    await server.close()


async def _prompt_after(kind: str, mgr: GoalManager) -> str:
    if kind == "continue":
        return (await mgr.evaluate_after_turn("step", judge_of(("continue", "more", False, False)))).prompt
    if kind == "blocked":
        return (await mgr.evaluate_after_turn("stuck", judge_of(("blocked", "needs a key", False, False)))).prompt
    if kind == "advisor":

        async def reviewer(goal):
            return True, ["the tests were not run"]

        return (
            await mgr.evaluate_after_turn("done!", judge_of(("done", "ok", False, False)), reviewer=reviewer)
        ).prompt
    assert kind == "empty"
    decision = None
    for _ in range(goals.MAX_CONSECUTIVE_EMPTY):
        decision = await mgr.evaluate_after_turn("", judge_of(("continue", "nothing said", False, False)))
    assert decision is not None
    return decision.prompt


@pytest.mark.parametrize("kind", ["continue", "blocked", "advisor", "empty"])
async def test_every_prompt_that_continues_a_goal_quotes_a_long_one(kind):
    mgr, _, _ = manager()
    long = ("Migrate everything. " + "part " * 300).strip()
    mgr.set(long)
    prompt = await _prompt_after(kind, mgr)
    assert long not in prompt and goals.quote(long) in prompt, kind


async def test_the_retry_prompt_of_a_failed_turn_quotes_a_long_goal_too(tmp_path, monkeypatch):
    from m1cmd_helpers import make_server, new_session

    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    mgr = server.goal_manager(live)
    long = ("Migrate everything. " + "part " * 300).strip()
    mgr.set(long)
    live.last_error = "boom"
    delay, prompt, notice = server._goal_recovery(live, mgr, "error")
    assert long not in prompt and goals.quote(long) in prompt
    await server.close()
