"""``autonomy.auto_continue``: an ordinary prompt runs as an implicit goal that ends cleared, never paused."""

from __future__ import annotations

import pytest

from k3code import autonomy
from k3code.agent.loop import AgentLoop
from k3code.automation.server_runner import ServerRunner
from k3code.autonomy.plan_first import PlanFirst
from k3code.autonomy.scope import ScopeVerdict
from k3code.goals import GoalState
from k3code.reliability import BudgetExceeded
from k3code.reliability.persistent_retry import TurnCancelled
from m1cmd_helpers import git_repo, make_server, new_session

AUTO = {"advisor_on_goal": False, "auto_continue": True}


def scripted_judge(verdicts):
    calls: list[tuple[str, str]] = []

    async def judge(goal: str, response: str, evidence: str = ""):
        calls.append((goal, response))
        v = verdicts[min(len(calls) - 1, len(verdicts) - 1)]
        return v, f"verdict {v}", False, False

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def _goal_notices(server) -> list[dict]:
    shown = [e["payload"] for e in server.event_log if e["type"] == "notification.show"]
    return [p for p in shown if p.get("kind") == "goal" or str(p.get("key", "")).startswith(("goal", "k3.goal"))]


def _implicit_goal_shown(server) -> bool:
    """The goal bar got an implicit goal at some point (so a test that ends with no goal saw one cleared)."""
    updates = [e["payload"] for e in server.event_log if e["type"] == "session.control.update"]
    return any(isinstance(g := u["control"].get("goal"), dict) and g.get("implicit") for u in updates)


async def _setup(tmp_path, monkeypatch, replies=None, autonomy=AUTO, **settings):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=replies, autonomy=autonomy, **settings)
    sid = await new_session(server, repo)
    live = server.live[sid]
    return server, provider, live, server.goal_manager(live)


async def test_an_ordinary_prompt_continues_until_the_judge_says_done_then_the_goal_is_cleared(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["step 1", "step 2", "all built"])
    judge = server.goal_judge = scripted_judge(["continue", "continue", "done"])
    status, text = await server._run_turn(live, "build the parser")
    assert status == "done" and text == "all built"
    assert provider.n == 3 and len(judge.calls) == 3
    assert {goal for goal, _ in judge.calls} == {"build the parser"}  # the prompt is the goal
    assert "build the parser" in str(provider.seen[1][-1].content)  # the continuation drives turn 2
    assert mgr.state is None  # cleared, not left "done" in the goal bar
    await server.close()


async def test_a_chat_reply_judged_done_is_one_turn_and_sends_no_goal_notice(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["hi there"])
    judge = server.goal_judge = scripted_judge(["done"])
    await server._run_turn(live, "say hello")
    assert provider.n == 1 and len(judge.calls) == 1
    assert mgr.state is None
    assert _goal_notices(server) == []
    await server.close()


async def test_a_blocked_verdict_clears_the_implicit_goal_without_a_resume_notice(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["I need the API key"])
    judge = server.goal_judge = scripted_judge(["blocked"])
    await server._run_turn(live, "deploy it")
    assert provider.n == 1 and len(judge.calls) == 1 and mgr.state is None
    assert _goal_notices(server) == []
    await server.close()


async def test_an_interrupted_implicit_goal_is_cleared_not_paused(tmp_path, monkeypatch):
    server, _, live, mgr = await _setup(tmp_path, monkeypatch)
    server.goal_judge = scripted_judge(["continue"])

    async def cancelled(self, *args, **kwargs):
        raise TurnCancelled("cancelled while parked")
        yield  # pragma: no cover

    monkeypatch.setattr(AgentLoop, "run", cancelled)
    status, _ = await server._run_turn(live, "refactor everything")
    assert status == "interrupted"
    assert mgr.state is None and _implicit_goal_shown(server)
    assert _goal_notices(server) == []
    await server.close()


async def test_needs_input_clears_the_implicit_goal(tmp_path, monkeypatch):
    server, _, live, mgr = await _setup(tmp_path, monkeypatch)
    server.goal_judge = scripted_judge(["continue"])

    async def over_budget(self, *args, **kwargs):
        raise BudgetExceeded("session token budget exceeded")
        yield  # pragma: no cover

    monkeypatch.setattr(AgentLoop, "run", over_budget)
    status, _ = await server._run_turn(live, "spend it all")
    assert status == "needs_input"
    assert mgr.state is None and _implicit_goal_shown(server)
    assert _goal_notices(server) == []
    await server.close()


async def test_a_real_goal_still_pauses_and_a_prompt_meanwhile_does_not_replace_it(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch)
    judge = server.goal_judge = scripted_judge(["continue"])
    mgr.set("ship the release")

    async def cancelled(self, *args, **kwargs):
        raise TurnCancelled("cancelled")
        yield  # pragma: no cover

    with monkeypatch.context() as m:
        m.setattr(AgentLoop, "run", cancelled)
        await server._run_turn(live, "go")
    state = mgr.state
    assert state.status == "paused" and state.paused_reason == "interrupted" and not state.implicit
    assert len(_goal_notices(server)) == 1

    await server._run_turn(live, "yes, use the staging key")  # the answer the paused goal waits for
    state = mgr.state
    assert state.goal == "ship the release" and state.status == "paused" and not state.implicit
    assert judge.calls == []  # a plain turn: nothing judged it
    await server.close()


async def test_auto_continue_off_makes_no_judge_call(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, autonomy={"advisor_on_goal": False})
    judge = server.goal_judge = scripted_judge(["done"])
    await server._run_turn(live, "next I would refactor")
    assert provider.n == 1 and judge.calls == []
    assert mgr.state is None and not _implicit_goal_shown(server)
    await server.close()


async def test_background_and_automation_turns_never_auto_continue(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch)
    judge = server.goal_judge = scripted_judge(["done"])
    result = await ServerRunner(server).run_prompt("tick", session_id=live.session_id)  # a loop/cron/automation run
    assert result.status == "completed"
    live.background = True  # /bg, Ctrl+B
    await server._run_turn(live, "background work")
    assert provider.n == 2 and judge.calls == []
    assert mgr.state is None and not _implicit_goal_shown(server)
    await server.close()


async def test_a_queued_prompt_replaces_the_implicit_goal(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["first", "second"])
    judge = server.goal_judge = scripted_judge(["done"])
    live.pending_prompts.append("actually, do this instead")  # typed while the first turn ran
    await server._run_turn(live, "first task")
    assert provider.n == 2
    assert [goal for goal, _ in judge.calls] == ["actually, do this instead"]  # the first one ended unjudged
    assert mgr.state is None
    await server.close()


async def test_a_queued_prompt_after_a_continued_turn_is_a_goal_of_its_own(tmp_path, monkeypatch):
    """A turn that ended right after a continuation left ``goal_continuation`` set; the queued prompt run next then
    counted as a continuation itself and got no implicit goal."""
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["step 1", "step 2", "third"])
    inner = scripted_judge(["continue", "done"])

    async def judge(goal, response, evidence=""):
        if not inner.calls:
            live.pending_prompts.append("then do this")  # typed while the first turn ran
        return await inner(goal, response, evidence)

    server.goal_judge = judge
    await server._run_turn(live, "first task")
    assert provider.n == 3
    # turn 2 ended with a prompt waiting, so it went unjudged; the waiting prompt is judged as the goal it now is
    assert inner.calls == [("first task", "step 1"), ("then do this", "third")]
    assert mgr.state is None
    await server.close()


async def test_kicks_never_resurrect_an_implicit_goal(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch)
    mgr.set("a prompt from before the daemon died", implicit=True)
    assert await server.watchdog_tick(source="boot") == []
    assert provider.n == 0 and mgr.state is None
    await server.close()


# ── the default: "auto" = on in the auto and yolo permission modes ──

#: the shipped default for auto_continue ("auto"); no scope gate, so the scripted replies are the turns' only calls
DEFAULT = {"advisor_on_goal": False, "plan_first": False}


def _classify_as(monkeypatch, scope: str) -> list[str]:
    """Stub the scope classifier; returns the prompts it was asked about."""
    asked: list[str] = []

    async def classify(self, session, text):
        asked.append(text)
        return ScopeVerdict(scope=scope, reason="stub")

    monkeypatch.setattr(PlanFirst, "_classify", classify)
    return asked


@pytest.mark.parametrize("mode", ["auto", "yolo"])
async def test_auto_and_yolo_mode_continue_an_unfinished_turn_by_default(tmp_path, monkeypatch, mode):
    server, provider, live, mgr = await _setup(
        tmp_path, monkeypatch, replies=["step 1", "all built"], autonomy=DEFAULT, permission_mode=mode
    )
    judge = server.goal_judge = scripted_judge(["continue", "done"])
    status, text = await server._run_turn(live, "build the parser")
    assert status == "done" and text == "all built"
    assert provider.n == 2 and [goal for goal, _ in judge.calls] == ["build the parser", "build the parser"]
    assert mgr.state is None
    await server.close()


async def test_default_mode_does_not_auto_continue_by_default(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["step 1"], autonomy=DEFAULT)
    assert live.perms.mode.value == "default"
    judge = server.goal_judge = scripted_judge(["continue"])
    await server._run_turn(live, "build the parser")
    assert provider.n == 1 and judge.calls == []
    assert mgr.state is None and not _implicit_goal_shown(server)
    await server.close()


async def test_a_prompt_the_scope_gate_rates_trivial_is_not_continued(tmp_path, monkeypatch):
    asked = _classify_as(monkeypatch, "trivial")
    autonomy = {"advisor_on_goal": False, "degrade_trivial": False, "proposals": False}
    server, provider, live, mgr = await _setup(
        tmp_path, monkeypatch, replies=["fixed the typo"], autonomy=autonomy, permission_mode="auto"
    )
    judge = server.goal_judge = scripted_judge(["continue"])
    await server._run_turn(live, "fix the typo in README line 3")
    assert asked == ["fix the typo in README line 3"]
    assert provider.n == 1 and judge.calls == []
    assert mgr.state is None and _implicit_goal_shown(server)  # it started, and ended unjudged after one turn
    await server.close()


async def test_a_plain_question_gets_no_implicit_goal(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(
        tmp_path, monkeypatch, replies=["it returns a dict"], autonomy=DEFAULT, permission_mode="auto"
    )
    judge = server.goal_judge = scripted_judge(["continue"])
    await server._run_turn(live, "what does parse() return?")
    assert provider.n == 1 and judge.calls == []
    assert mgr.state is None and not _implicit_goal_shown(server)
    await server.close()


async def test_continuations_of_an_implicit_goal_skip_the_scope_gate(tmp_path, monkeypatch):
    asked = _classify_as(monkeypatch, "small")
    autonomy = {"advisor_on_goal": False, "proposals": False}
    server, provider, live, _ = await _setup(
        tmp_path, monkeypatch, replies=["step 1", "all built"], autonomy=autonomy, permission_mode="auto"
    )
    server.goal_judge = scripted_judge(["continue", "done"])
    await server._run_turn(live, "build the parser")
    assert provider.n == 2 and asked == ["build the parser"]  # gated once, not again for the continuation
    await server.close()


async def test_continuations_of_an_explicit_goal_skip_the_scope_gate_too(tmp_path, monkeypatch):
    """A /goal's continuation turns re-ran the scope gate (classifier, and for a medium task the strong-tier planner,
    advisor and proposer) every turn. Like an implicit goal's, they are the same task: gated once, on the first turn."""
    asked = _classify_as(monkeypatch, "small")
    autonomy = {"advisor_on_goal": False, "proposals": False}
    server, provider, live, mgr = await _setup(
        tmp_path, monkeypatch, replies=["step 1", "step 2", "all built"], autonomy=autonomy, permission_mode="auto"
    )
    server.goal_judge = scripted_judge(["continue", "continue", "done"])
    mgr.set("build the parser")
    status, text = await server._run_turn(live, "go")
    assert status == "done" and text == "all built"
    assert provider.n == 3 and asked == ["go"]  # gated once, not again for the two continuations
    assert mgr.state is not None and mgr.state.status == "done" and not mgr.state.implicit
    await server.close()


async def test_the_system_prompt_is_byte_identical_across_the_turns_of_an_implicit_goal(tmp_path, monkeypatch):
    """A continuation turn must reuse the first turn's system prompt byte for byte: any difference (a clock, a goal
    line, a reordered section) breaks the provider's prompt cache on every judged turn."""
    server, provider, live, _ = await _setup(
        tmp_path, monkeypatch, replies=["step 1", "all built"], autonomy=DEFAULT, permission_mode="auto"
    )
    server.goal_judge = scripted_judge(["continue", "done"])
    await server._run_turn(live, "build the parser")
    assert provider.n == 2
    first, second = provider.seen[0][0], provider.seen[1][0]
    assert first.role == "system" and second.role == "system"
    assert first.content == second.content
    await server.close()


async def test_the_turn_cap_ends_an_implicit_goal_with_a_notice(tmp_path, monkeypatch):
    autonomy = {**DEFAULT, "auto_continue_max_turns": 2}
    server, provider, live, mgr = await _setup(
        tmp_path, monkeypatch, replies=["a", "b", "c"], autonomy=autonomy, permission_mode="auto"
    )
    judge = server.goal_judge = scripted_judge(["continue"])
    await server._run_turn(live, "build everything")
    assert provider.n == 2 and len(judge.calls) == 2
    assert mgr.state is None
    assert [n["text"] for n in _goal_notices(server)][-1].startswith("↻ Auto-continue stopped at its 2-turn cap")
    await server.close()


def test_auto_continue_setting_resolves_per_permission_mode():
    auto_continue_on, DEFAULTS = autonomy.auto_continue_on, autonomy.DEFAULTS  # noqa: N806
    for mode in ("auto", "yolo"):
        assert auto_continue_on({"auto_continue": "auto"}, mode)
    for mode in ("default", "accept-edits", "plan"):
        assert not auto_continue_on({"auto_continue": "auto"}, mode)
        assert auto_continue_on({"auto_continue": True}, mode)
    assert not auto_continue_on({"auto_continue": False}, "yolo")
    assert DEFAULTS["auto_continue"] == "auto" and DEFAULTS["auto_continue_max_turns"] == 8


def test_plain_question_is_narrow():
    is_plain_question = autonomy.is_plain_question
    assert is_plain_question("what does parse() return?")
    assert not is_plain_question("can you fix the parser?")
    assert not is_plain_question("Please add tests?")
    assert not is_plain_question("fix the parser")
    assert not is_plain_question("look at this:\nwhy does it fail?")


def test_implicit_is_persisted_in_the_snapshot_and_old_states_load_without_it():
    state = GoalState(goal="g", implicit=True)
    assert GoalState.from_dict(state.to_dict()).implicit is True
    assert state.snapshot()["implicit"] is True
    old = state.to_dict()
    del old["implicit"]
    assert GoalState.from_dict(old).implicit is False


async def test_a_judge_reply_that_is_not_a_verdict_ends_auto_continue_after_one_turn(tmp_path, monkeypatch):
    from k3code.goals import make_judge

    server, provider, live, mgr = await _setup(
        tmp_path, monkeypatch, replies=["step 1", "step 2"], autonomy=DEFAULT, permission_mode="auto"
    )
    replies: list[str] = []

    async def complete(system, user):
        replies.append(user)
        return "Sure! The agent made good progress."  # no JSON verdict

    server.goal_judge = make_judge(complete)
    await server._run_turn(live, "build the parser")
    assert provider.n == 1 and len(replies) == 1  # no unattended turn on an unreadable verdict
    assert mgr.state is None
    assert [n["text"] for n in _goal_notices(server)][-1].startswith("↻ Auto-continue stopped: the completion judge")
    await server.close()


async def test_a_bad_turn_cap_set_at_runtime_falls_back_to_the_default_instead_of_failing_the_prompt(
    tmp_path, monkeypatch
):
    """The cap was read with ``int(...)`` at prompt time: a value that bypassed the config-load check (set at runtime,
    or a typo) raised and the prompt failed."""
    server, provider, live, mgr = await _setup(
        tmp_path, monkeypatch, replies=["a"], autonomy=dict(DEFAULT), permission_mode="auto"
    )
    judge = server.goal_judge = scripted_judge(["continue"])
    for bad in ("eight", -1, None):
        server.config.autonomy["auto_continue_max_turns"] = bad
        status, _ = await server._run_turn(live, "build everything")
        assert status == "done"
    assert len(judge.calls) == 3 * 8  # each prompt ran to the default 8-turn cap
    server.config.autonomy["auto_continue"] = "sometimes"  # invalid: the default "auto" (on in auto mode)
    server.config.autonomy["auto_continue_max_turns"] = "2"
    await server._run_turn(live, "build everything")
    assert len(judge.calls) == 3 * 8 + 2
    await server.close()


# ── /undo after an auto-continued run ──


async def _undo(server, live) -> dict:
    from m1cmd_helpers import rpc

    return (await rpc(server, "session.undo", {"session_id": live.session_id}))["result"]


def _conversation(messages) -> list[dict]:
    """The stored messages without the system entry (each turn re-stores it)."""
    return [m for m in messages if m["role"] != "system"]


async def test_undo_after_an_auto_continued_run_drops_the_whole_run_back_to_the_users_prompt(tmp_path, monkeypatch):
    """Every continuation is stored as a user message: /undo dropped only the last continuation turn, and the run's
    earlier turns stayed in the conversation the next prompt (and /retry) went on from."""
    from k3code.providers.types import messages_to_anthropic, messages_to_openai

    server, provider, live, mgr = await _setup(
        tmp_path, monkeypatch, replies=["hello back", "step 1", "step 2", "all built"], autonomy={**AUTO}
    )
    server.goal_judge = scripted_judge(["continue", "continue", "done"])
    server.config.autonomy["auto_continue"] = False
    await server._run_turn(live, "first question")  # a normal turn: no goal
    kept = _conversation(live.messages)
    server.config.autonomy["auto_continue"] = True
    await server._run_turn(live, "build the parser")
    assert provider.n == 4 and mgr.state is None
    run = _conversation(live.messages)[len(kept) :]
    assert [m["role"] for m in run] == ["user", "assistant"] * 3
    assert [bool((m.get("meta") or {}).get("continuation")) for m in run[0::2]] == [False, True, True]
    sent = provider.seen[-1]  # the marker stays in the store: it never reaches a model
    assert all("meta" not in entry for entry in messages_to_openai(sent))
    assert "continuation" not in str(messages_to_anthropic(sent))

    assert await _undo(server, live) == {"removed": 6}  # the user's prompt, two continuations and three replies
    assert _conversation(live.messages) == kept
    assert _conversation(server.store.get(live.session_id).messages) == kept  # persisted
    assert await _undo(server, live) == {"removed": 2}  # a normal turn: its prompt and reply, as before
    assert await _undo(server, live) == {"removed": 0}
    await server.close()


def _outcomes(server) -> dict[str, int]:
    """Each recorded turn's ``undone`` flag, oldest first."""
    return [r["undone"] for r in server.learning.log.turn_outcomes(since=0)]


async def test_undo_after_an_auto_continued_run_marks_every_turn_of_the_run_undone(tmp_path, monkeypatch):
    """K3NOX-696: /undo removed the whole run, but only its first turn row was marked undone; the continuation turns
    kept ``passed`` and the optimizer's success rate still counted them."""
    server, _, live, _ = await _setup(
        tmp_path, monkeypatch, replies=["hello back", "step 1", "step 2", "all built"], autonomy={**AUTO}
    )
    server.goal_judge = scripted_judge(["continue", "continue", "done"])
    server.config.autonomy["auto_continue"] = False
    await server._run_turn(live, "first question")
    server.config.autonomy["auto_continue"] = True
    await server._run_turn(live, "build the parser")
    assert _outcomes(server) == [0, 0, 0, 0]  # the normal turn, the run's turn and its two continuations
    assert await _undo(server, live) == {"removed": 6}
    assert _outcomes(server) == [0, 1, 1, 1]
    undo = server.learning.log.query("undo")[0]
    assert undo["subject"] == server.learning.log.turn_outcomes(since=0)[1]["turn"]  # logged under the run's prompt
    assert await _undo(server, live) == {"removed": 2}
    assert _outcomes(server) == [0, 1, 1, 1]  # the run's prompt settled the normal turn: it was kept, as before
    await server.close()


async def test_retry_after_an_auto_continued_run_marks_every_turn_of_the_run_undone(tmp_path, monkeypatch):
    """/retry is an /undo (``session.undo``) followed by sending the prompt again: the retried run's turns are all
    undone, the new attempt's turn is not."""
    server, _, live, _ = await _setup(tmp_path, monkeypatch, replies=["step 1", "step 2", "all built", "built again"])
    server.goal_judge = scripted_judge(["continue", "continue", "done", "done"])
    await server._run_turn(live, "build the parser")
    assert _outcomes(server) == [0, 0, 0]
    assert await _undo(server, live) == {"removed": 6}
    await server._run_turn(live, "build the parser")
    assert _outcomes(server) == [1, 1, 1, 0]
    await server.close()


async def test_undo_after_a_goal_run_drops_its_continuations_too(tmp_path, monkeypatch):
    server, provider, live, mgr = await _setup(tmp_path, monkeypatch, replies=["step 1", "all built"])
    server.goal_judge = scripted_judge(["continue", "done"])
    mgr.set("ship the release")
    await server._run_turn(live, "go")
    assert provider.n == 2
    assert await _undo(server, live) == {"removed": 4}
    assert _conversation(live.messages) == []
    await server.close()
