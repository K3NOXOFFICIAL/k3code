"""Wake words and the ultracode mode through the real gateway: which prompts become a job, and which never do.

The three pipelines are stubbed (``Pipeline``) where only the routing matters; /ultraplan runs for real on the scripted
provider. Nothing here touches a real home: ``make`` points K3CODE_HOME beside the tmp dir."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from k3code.config import Settings, load_config
from k3code.wakewords import detect_enabled, wake_cfg
from test_autonomy_gateway import call, events, k3home, make, models_called, start, sub, verdict
from test_ultra import JUDGE, PLANNERS

NORMAL = {"type": "text", "model": "m-main", "text": "NORMAL-TURN"}
NORMAL_ANY = {"type": "text", "text": "NORMAL-TURN"}  # also for a background turn, which runs on the cheap tier
NO_GATE = {"plan_first": False}  # the scope gate stays out of the way unless a test is about it
MODES = ("ultracode", "ultraplan", "ultraresearch")


class Pipeline:
    """Stands in for ultracode and ultraresearch: records what ran, can be held open, can leave a child behind."""

    def __init__(self, server: Any, monkeypatch: pytest.MonkeyPatch, *, research_reason: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.sessions: list[Any] = []
        self.hold: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.orphan = False  # leave a never-ending sub-agent behind, as a pipeline that was cut off would
        self.server = server

        async def ultracode(session: Any, task: str) -> str:
            return await self.run("ultracode", session, task)

        async def research(session: Any, question: str, *, n_sub: int | None = None) -> Any:
            return SimpleNamespace(report=await self.run("ultraresearch", session, question), path="r.md")

        async def unavailable_reason() -> str:
            return research_reason

        monkeypatch.setattr(server.ultra, "ultracode", ultracode)
        monkeypatch.setattr(server.research, "run", research)
        server.research_tools = SimpleNamespace(name="stub tools", unavailable_reason=unavailable_reason)

    async def run(self, mode: str, session: Any, task: str) -> str:
        self.calls.append((mode, task))
        self.sessions.append(session)
        if self.orphan:
            self.server.subagents.spawn(session, description="orphan", prompt="never ends")
            await asyncio.sleep(0.05)  # let the child start: one cancelled before its first step is never marked
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        return f"{mode} result: {task}"


def answer(mode: str, task: str) -> str:
    """The assistant message the stubbed pipeline ends with (ultraresearch appends where it saved the report)."""
    text = f"{mode} result: {task}"
    return text + "\n\nReport saved: r.md" if mode == "ultraresearch" else text


def pipeline(server: Any, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Pipeline:
    return Pipeline(server, monkeypatch, **kw)


async def finish(server: Any) -> None:
    await asyncio.wait_for(server.session.turn_task, 20)
    await server.autonomy.drain()


async def submit(server: Any, text: str, **params: Any) -> dict:
    out = await call(server, "prompt.submit", {"text": text, **params})
    await finish(server)
    return out


def transcript(server: Any) -> list[tuple[str, str]]:
    return [(m["role"], m.get("content") or "") for m in server.session.stored.messages]


def progress(server: Any) -> list[dict]:
    return events(server, "ultra.progress")


def classifier_calls(server: Any) -> int:
    return sum("You classify a coding task" in c["text"] for p in server.providers for c in p.log)


@pytest.fixture
def hung_children(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sub-agent that never ends on its own (no model behind it): only the end of its job can stop it."""

    async def hang(self: Any, parent: Any, h: Any, atype: Any, prompt: str, cwd: Any) -> None:
        await asyncio.sleep(3600)

    from k3code.subagents.runner import SubagentManager

    monkeypatch.setattr(SubagentManager, "_drive", hang)


# ── wake words ──


@pytest.mark.parametrize(
    ("text", "mode", "task"),
    [
        ("ultracode fix the failing tests", "ultracode", "fix the failing tests"),
        ("Ultraresearch: how does asyncio.timeout work?", "ultraresearch", "how does asyncio.timeout work?"),
    ],
)
async def test_a_wake_word_runs_its_mode_once_with_the_word_removed(tmp_path, monkeypatch, text, mode, task):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    out = await submit(server, text)
    assert out["status"] == "streaming"
    assert pipe.calls == [(mode, task)]
    first = progress(server)[0]
    assert (first["command"], first["phase"], first["detail"]) == (mode, "wake word", f'"{mode}" in your message')
    # the transcript keeps what the user typed, not the word-stripped task or the job label
    assert transcript(server) == [("user", text), ("assistant", answer(mode, task))]
    done = events(server, "message.complete")[-1]
    assert done["status"] == "done" and done["text"] == answer(mode, task)
    assert models_called(server) == []  # no normal turn on the side


async def test_ultraplan_wake_word_runs_the_real_pipeline_and_go_still_works(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [*PLANNERS, JUDGE], autonomy=NO_GATE)
    await start(server, tmp_path)
    await submit(server, "I want you to ultraplan add three files")
    assert progress(server)[0]["phase"] == "wake word" and progress(server)[0]["command"] == "ultraplan"
    assert "planning" in [p["phase"] for p in progress(server)]
    role, answer = transcript(server)[-1]
    assert role == "assistant" and answer.startswith("Plan for: I want you to add three files")
    assert transcript(server)[0] == ("user", "I want you to ultraplan add three files")
    # the same hand-over as /ultraplan: /go executes the saved plan
    assert server.session.stored.meta["ultra_plan"]["task"] == "I want you to add three files"
    go = await call(server, "slash.exec", {"command": "go"})
    assert go["type"] == "send" and server.session.preapproved_plan is not None


@pytest.mark.parametrize("mode", MODES)
async def test_a_bare_wake_word_answers_with_the_usage_line_and_runs_nothing(tmp_path, monkeypatch, mode):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    await submit(server, mode.capitalize())
    usage = {"ultracode": "/ultracode <task>", "ultraplan": "/ultraplan <task>"}.get(
        mode, "/ultraresearch [--n N] <question>"
    )
    assert transcript(server) == [("user", mode.capitalize()), ("assistant", f"Usage: {usage}")]
    assert pipe.calls == [] and progress(server) == [] and models_called(server) == []
    assert server.subagents.handles == {}
    assert events(server, "message.complete")[-1]["status"] == "done"


async def test_a_wake_word_for_an_unavailable_research_tool_says_so_and_runs_nothing(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch, research_reason="no search server")
    await start(server, tmp_path)
    await submit(server, "ultraresearch what changed in 3.14?")
    assert pipe.calls == [] and progress(server) == []
    assert "unavailable: no search server" in transcript(server)[-1][1]


@pytest.mark.parametrize(
    "text",
    [
        "what does `ultracode` do?",
        'the word "ultraplan" is a wake word',
        "compare ultracode and ultraplan for me",
        "read src/ultracode.py and explain it",
        "/note ultraresearch this later",
        "see the tag #ultraplan and the flag --ultracode",
    ],
)
async def test_quoted_ambiguous_and_slash_text_is_a_normal_turn(tmp_path, monkeypatch, text):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    await submit(server, text)
    assert pipe.calls == [] and progress(server) == []
    assert transcript(server)[-1] == ("assistant", "NORMAL-TURN")


async def test_a_queued_prompt_with_a_wake_word_runs_the_mode_when_it_drains(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    pipe.hold = asyncio.Event()
    await call(server, "prompt.submit", {"text": "ultracode build the thing"})
    task = server.session.turn_task
    await pipe.started.wait()
    queued = await call(server, "prompt.submit", {"text": "ultraresearch how does it work?"})
    assert queued["status"] == "queued"
    assert pipe.calls == [("ultracode", "build the thing")]
    pipe.hold.set()
    await asyncio.wait_for(task, 20)
    assert pipe.calls == [("ultracode", "build the thing"), ("ultraresearch", "how does it work?")]
    assert [t for t in transcript(server) if t[0] == "user"] == [
        ("user", "ultracode build the thing"),
        ("user", "ultraresearch how does it work?"),
    ]


async def test_a_steering_message_no_loop_took_runs_its_wake_word_when_it_drains(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    pipe.hold = asyncio.Event()
    await call(server, "prompt.submit", {"text": "ultracode build the thing"})
    task = server.session.turn_task
    await pipe.started.wait()
    assert (await call(server, "session.steer", {"text": "ultraresearch while you work: is x faster?"}))["steered"]
    assert (await call(server, "session.steer", {"text": "ultraplan from the tui", "automated": True}))["steered"]
    pipe.hold.set()
    await asyncio.wait_for(task, 20)
    assert pipe.calls == [("ultracode", "build the thing"), ("ultraresearch", "while you work: is x faster?")]
    assert transcript(server)[-1] == ("assistant", "NORMAL-TURN")  # the automated one ran as a plain prompt


async def test_a_slash_job_drains_a_queued_wake_word_prompt(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    pipe.hold = asyncio.Event()
    await call(server, "slash.exec", {"command": "ultracode build the thing"})
    task = server.session.turn_task
    await pipe.started.wait()
    assert (await call(server, "prompt.submit", {"text": "ultraresearch and then this"}))["status"] == "queued"
    pipe.hold.set()
    await asyncio.wait_for(task, 20)
    assert pipe.calls == [("ultracode", "build the thing"), ("ultraresearch", "and then this")]


@pytest.mark.parametrize("via", ["rpc", "slash"])
async def test_bg_with_a_wake_word_runs_the_mode_in_the_background_session(tmp_path, monkeypatch, via):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    if via == "rpc":
        sid = (await call(server, "prompt.background", {"text": "ultracode do it in the background"}))["session_id"]
    else:
        await call(server, "slash.exec", {"command": "bg ultracode do it in the background"})
        sid = next(s.session_id for s in server.live.values() if s.background)
    live = server.live[sid]
    await asyncio.wait_for(live.turn_task, 20)
    assert pipe.calls == [("ultracode", "do it in the background")]
    assert pipe.sessions[-1] is live and live.background and live is not server.session
    assert live.stored.messages[0] == {"role": "user", "content": "ultracode do it in the background"}


async def test_goal_kicks_and_unattended_prompts_never_trigger(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    live = server.session
    live.ultra_mode = "ultracode"  # not even the mode applies to them

    async def done(goal: str, last: str) -> tuple[str, str, bool, bool]:
        return "done", "ok", False, False

    server.goal_judge = done  # type: ignore[assignment]
    server.goal_manager(live).set("ultracode the whole repo")
    assert await server.kick_goal(live, source="test")
    await asyncio.wait_for(live.turn_task, 20)
    # a loop / cron / automation tick is a plain-string turn too (ServerRunner.run_prompt)
    await server._run_turn(live, "ultraplan the weekly report", drain=False)
    assert pipe.calls == [] and progress(server) == []
    assert classifier_calls(server) == 0
    assert [t for t in transcript(server) if t[0] == "assistant"].count(("assistant", "NORMAL-TURN")) >= 2


async def test_automated_prompts_skip_wake_words_and_the_mode_now_and_from_the_queue(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("large"), NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "ultracode a skill expansion", automated=True)
    await submit(server, "rewrite the whole thing", automated=True)  # the mode would route this one
    assert pipe.calls == [] and classifier_calls(server) == 0
    assert [t[1] for t in transcript(server) if t[0] == "assistant"] == ["NORMAL-TURN", "NORMAL-TURN"]
    # the same prompts waiting behind a running job are still skipped when they drain
    pipe.hold = asyncio.Event()
    await call(server, "prompt.submit", {"text": "ultracode first"})
    task = server.session.turn_task
    await pipe.started.wait()
    await call(server, "prompt.submit", {"text": "ultraresearch typed while waiting"})
    await call(server, "prompt.submit", {"text": "ultraplan generated by the tui", "automated": True})
    await call(server, "prompt.submit", {"text": "rewrite the whole product", "automated": True})
    assert [type(p).__name__ for p in server.session.pending_prompts] == ["TypedPrompt", "str", "str"]
    pipe.hold.set()
    await asyncio.wait_for(task, 20)
    assert pipe.calls == [("ultracode", "first"), ("ultraresearch", "typed while waiting")]
    assert [t[1] for t in transcript(server) if t[0] == "assistant"][-2:] == ["NORMAL-TURN", "NORMAL-TURN"]
    assert classifier_calls(server) == 0  # neither the word nor the mode looked at the automated ones


async def test_wake_words_can_be_switched_off_all_or_one_by_one(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE, wake_words={"enabled": False})
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    await submit(server, "ultracode fix it")
    assert pipe.calls == [] and transcript(server)[-1] == ("assistant", "NORMAL-TURN")

    server = make(sub(tmp_path, "b"), monkeypatch, [NORMAL], autonomy=NO_GATE, wake_words={"ultraplan": False})
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    await submit(server, "ultraplan the migration")
    assert pipe.calls == [] and transcript(server)[-1] == ("assistant", "NORMAL-TURN")
    await submit(server, "ultracode the migration")
    assert pipe.calls == [("ultracode", "the migration")]


async def test_an_inline_job_is_stopped_by_stop_and_leaves_no_sub_agents(tmp_path, monkeypatch, hung_children):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    pipe.orphan = True
    await start(server, tmp_path)
    pipe.hold = asyncio.Event()
    await call(server, "prompt.submit", {"text": "ultracode a long job"})
    task = server.session.turn_task
    await pipe.started.wait()
    assert server.session.streaming and server.session.state == "working"
    assert (await call(server, "session.interrupt", {}))["interrupted"] is True
    await asyncio.wait_for(task, 20)
    done = events(server, "message.complete")[-1]
    assert done["status"] == "interrupted" and "interrupted" in done["text"]
    assert not server.session.streaming and server.session.state == "completed"
    assert [h.status for h in server.subagents.for_session(server.session.session_id)] == ["interrupted"]
    assert transcript(server)[0] == ("user", "ultracode a long job")


async def test_stopping_a_backgrounded_job_says_stopped_not_finished(tmp_path, monkeypatch, hung_children):
    # Issue #56: the job catches the /stop and returns "interrupted", so the task ends normally and the background
    # watcher used to report "finished".
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    pipe.hold = asyncio.Event()
    await call(server, "prompt.submit", {"text": "ultracode a long job"})
    job = server.session
    task = job.turn_task
    await pipe.started.wait()
    assert (await call(server, "prompt.background", {}))["status"] == "backgrounded"
    assert job.background and server.session is not job
    assert (await call(server, "session.interrupt", {"session_id": job.session_id}))["interrupted"] is True
    await asyncio.wait_for(task, 20)
    await asyncio.sleep(0.05)  # done-callbacks run on the next loop iteration
    note = [n for n in events(server, "notification.show") if n.get("kind") == "background"]
    assert len(note) == 1 and "was stopped" in note[0]["text"] and "finished" not in note[0]["text"]
    assert note[0]["level"] == "warning"


async def test_a_job_that_ends_takes_its_sub_agents_with_it(tmp_path, monkeypatch, hung_children):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    pipe.orphan = True
    await start(server, tmp_path)
    await submit(server, "ultracode finish while a child still runs")
    for _ in range(100):  # the child unwinds on the next loop turns
        if all(h.done for h in server.subagents.for_session(server.session.session_id)):
            break
        await asyncio.sleep(0.01)
    assert [h.status for h in server.subagents.for_session(server.session.session_id)] == ["interrupted"]


async def test_a_job_failure_is_reported_and_the_session_stays_usable(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)

    async def boom(session: Any, task: str) -> str:
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(server.ultra, "ultracode", boom)
    await start(server, tmp_path)
    await submit(server, "ultracode do the thing")
    done = events(server, "message.complete")[-1]
    assert done["status"] == "error" and "pipeline exploded" in done["text"]
    await submit(server, "a normal follow-up")
    assert transcript(server)[-1] == ("assistant", "NORMAL-TURN")


# ── the ultracode mode ──


async def test_mode_on_runs_a_prompt_of_at_least_min_scope_through_ultracode(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("small"), NORMAL])
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "add a --verbose flag to the CLI")
    assert pipe.calls == [("ultracode", "add a --verbose flag to the CLI")]  # the whole text, no word to strip
    first = progress(server)[0]
    assert (first["command"], first["phase"], first["detail"]) == ("ultracode", "ultracode is on", "scope small")
    assert transcript(server)[0] == ("user", "add a --verbose flag to the CLI")
    assert classifier_calls(server) == 1 and events(server, "scope.verdict") == []  # the gate did not run


async def test_mode_on_a_trivial_prompt_is_a_normal_turn_and_is_classified_once(tmp_path, monkeypatch):
    server = make(
        tmp_path, monkeypatch, [verdict("trivial"), NORMAL], autonomy={"degrade_trivial": False, "proposals": False}
    )
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "what does the retry flag do?")
    assert pipe.calls == [] and progress(server) == []
    assert transcript(server)[-1] == ("assistant", "NORMAL-TURN")
    # the gate used the mode's verdict instead of asking the classifier again, and still logged and showed it
    assert classifier_calls(server) == 1
    shown = events(server, "scope.verdict")
    assert len(shown) == 1 and shown[0]["scope"] == "trivial" and shown[0]["source"] == "classifier"


async def test_mode_on_a_failed_classifier_is_a_normal_turn_and_is_not_retried_by_the_gate(tmp_path, monkeypatch):
    steps = [{"type": "error", "model": "m-cheap", "status_code": 400, "message": "nope"}, NORMAL]
    server = make(tmp_path, monkeypatch, steps, autonomy={"proposals": False})
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "refactor the auth module")
    assert pipe.calls == [] and transcript(server)[-1] == ("assistant", "NORMAL-TURN")
    shown = events(server, "scope.verdict")
    assert len(shown) == 1 and shown[0]["source"] == "fallback"
    assert classifier_calls(server) == 2  # one attempt: the cheap tier, then main took over; not four


async def test_mode_off_never_classifies_for_it(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("huge"), NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    await submit(server, "rewrite the whole product")
    assert pipe.calls == [] and classifier_calls(server) == 0


async def test_min_scope_from_the_config_is_honoured(tmp_path, monkeypatch):
    server = make(
        tmp_path, monkeypatch, [verdict("medium"), NORMAL], autonomy=NO_GATE, ultracode={"min_scope": "large"}
    )
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "add rate limiting")
    assert pipe.calls == [] and transcript(server)[-1] == ("assistant", "NORMAL-TURN")
    server = make(
        sub(tmp_path, "c"), monkeypatch, [verdict("large"), NORMAL], autonomy=NO_GATE, ultracode={"min_scope": "large"}
    )
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "add a notifications system")
    assert pipe.calls == [("ultracode", "add a notifications system")]


async def test_mode_skips_background_sessions_and_the_go_after_ultraplan(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("huge"), NORMAL_ANY], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    live = server.session
    live.ultra_mode = "ultracode"
    # /go after /ultraplan: the plan is approved, the prompt goes to the gate and fan-out, not to the pipeline again
    live.preapproved_plan = {"task": "do x", "plan": "## Goal\nx\n## Steps\n1. do x", "path": ""}
    await submit(server, "do x")
    assert pipe.calls == [] and classifier_calls(server) == 0
    assert transcript(server)[-1] == ("assistant", "NORMAL-TURN")
    # a background session (here: /bg from a session whose mode is on, which the new session inherits)
    sid = (await call(server, "prompt.background", {"text": "rewrite the whole product"}))["session_id"]
    bg = server.live[sid]
    await asyncio.wait_for(bg.turn_task, 20)
    assert bg.background and pipe.calls == [] and classifier_calls(server) == 0
    assert bg.stored.messages[-1]["content"] == "NORMAL-TURN"
    # ... and a prompt submitted with background=true
    server.session.ultra_mode = "ultracode"
    await submit(server, "rewrite the whole product again", background=True)
    assert pipe.calls == [] and classifier_calls(server) == 0


async def test_mode_skips_slash_looking_text_and_a_wake_word_wins_over_the_mode(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("large"), NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "/unknown thing to do")
    assert pipe.calls == [] and classifier_calls(server) == 0
    await submit(server, "ultraresearch the options")  # explicit beats implicit, and costs no classification
    assert pipe.calls == [("ultraresearch", "the options")] and classifier_calls(server) == 0


async def test_mode_follows_a_scope_override_and_spends_it(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("trivial"), NORMAL])
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await call(server, "command.dispatch", {"name": "scope", "arg": "large"})
    await submit(server, "rename x")
    assert pipe.calls == [("ultracode", "rename x")] and classifier_calls(server) == 0
    assert server.session.scope_override is None  # spent: it must not hit the next task


async def test_a_queued_typed_prompt_is_routed_with_the_mode_as_it_is_then(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("small"), NORMAL])
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    live = server.session
    pipe.hold = asyncio.Event()
    await call(server, "prompt.submit", {"text": "ultracode first"})
    task = live.turn_task
    await pipe.started.wait()
    await call(server, "prompt.submit", {"text": "fix the parser"})  # queued while the mode was off
    live.ultra_mode = "ultracode"  # switched on before it drains
    pipe.hold.set()
    await asyncio.wait_for(task, 20)
    assert pipe.calls == [("ultracode", "first"), ("ultracode", "fix the parser")]


async def test_stop_during_the_scope_check_ends_the_turn_cleanly(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("large"), NORMAL])
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    live = server.session
    live.ultra_mode = "ultracode"
    checking = asyncio.Event()

    async def slow_verdict(session: Any, text: str) -> Any:
        checking.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(server.autonomy, "mode_verdict", slow_verdict)
    await call(server, "prompt.submit", {"text": "rewrite the whole product"})
    task = live.turn_task
    await checking.wait()
    assert (await call(server, "session.interrupt", {}))["interrupted"] is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    assert live.run_result == "completed" and live.state == "completed" and not live.turn_in_flight
    assert events(server, "status.update")[-1]["text"] == ""  # the "checking scope" line is cleared
    assert pipe.calls == [] and progress(server) == []


async def test_a_routing_failure_runs_the_prompt_as_a_normal_turn(tmp_path, monkeypatch, caplog):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)

    async def broken(ctx: Any, live: Any, arg: str) -> Any:
        raise RuntimeError("prepare exploded")

    monkeypatch.setattr(server.commands.get("ultracode"), "prepare", broken, raising=False)
    with caplog.at_level(logging.ERROR, logger="k3code.gateway"):
        await submit(server, "ultracode fix the build")
    assert pipe.calls == [] and transcript(server)[-1] == ("assistant", "NORMAL-TURN")  # the prompt is not lost
    assert "routing failed" in caplog.text


# ── /ultracode and the other slash commands ──


async def test_ultracode_command_toggles_sets_and_shows_the_mode(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [])
    await start(server, tmp_path)
    live = server.session

    async def run(arg: str) -> str:
        return (await call(server, "slash.exec", {"command": f"ultracode {arg}".strip()}))["output"]

    assert (await run("status")).startswith("Ultracode mode is off") and live.ultra_mode == "off"
    assert (await run("")).startswith("Ultracode mode is on")  # bare toggles
    assert live.ultra_mode == "ultracode" and live.stored.meta["ultra_mode"] == "ultracode"
    assert events(server, "session.info")[-1]["ultra_mode"] == "ultracode"
    assert (await run("on")).startswith(
        "Ultracode mode is on"
    ) and live.ultra_mode == "ultracode"  # a set, not a toggle
    assert (await run("STATUS")).startswith("Ultracode mode is on")
    assert (await run("")).startswith("Ultracode mode is off")
    assert live.ultra_mode == "off" and "ultra_mode" not in live.stored.meta
    assert events(server, "session.info")[-1]["ultra_mode"] == "off"
    await run("off")
    assert live.ultra_mode == "off"
    await run("on")
    assert (await run("off")).startswith("Ultracode mode is off") and live.ultra_mode == "off"
    out = await call(server, "slash.exec", {"command": "ultracode on", "session_id": "nope"})
    assert out["output"] == "No active session."


async def test_ultracode_with_a_task_stays_a_one_shot_that_does_not_flip_the_mode(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [])
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    out = await call(server, "slash.exec", {"command": "ultracode fix the build"})
    assert out["output"].startswith("ultracode started: plan")
    await finish(server)
    assert pipe.calls == [("ultracode", "fix the build")] and server.session.ultra_mode == "off"
    assert progress(server) == []  # no wake-word line: it was typed as a command
    assert transcript(server)[0] == ("user", "/ultracode fix the build")
    server.session.ultra_mode = "ultracode"
    await call(
        server, "slash.exec", {"command": "ultracode on the way to the airport"}
    )  # starts with a word, is a task
    await finish(server)
    assert pipe.calls[-1] == ("ultracode", "on the way to the airport") and server.session.ultra_mode == "ultracode"


async def test_slash_commands_keep_their_usage_lines_and_acks(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [])
    pipeline(server, monkeypatch)
    await start(server, tmp_path)

    async def run(line: str) -> str:
        return (await call(server, "slash.exec", {"command": line}))["output"]

    assert await run("ultraplan") == "Usage: /ultraplan <task>"
    assert await run("ultraresearch") == "Usage: /ultraresearch [--n N] <question>"
    assert (await run("ultraresearch --n 2 what is x")).startswith("Researching with stub tools: decompose")
    await finish(server)
    assert (await call(server, "slash.exec", {"command": "ultracode x", "session_id": "nope"}))["output"] == (
        "No active session."
    )


async def test_ultraresearch_takes_a_question_with_an_apostrophe(tmp_path, monkeypatch):
    """shlex failed on a lone quote ('No closing quotation'), so a natural question could not be asked."""
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    out = await call(server, "slash.exec", {"command": "ultraresearch what's the state of asyncio?"})
    assert out["output"].startswith("Researching with")
    await finish(server)
    await submit(server, "ultraresearch --n 3 what's new in 3.14?")
    assert pipe.calls == [("ultraresearch", "what's the state of asyncio?"), ("ultraresearch", "what's new in 3.14?")]


# ── typed "ultracode off" and friends are mode control, not a task ──


@pytest.mark.parametrize(
    ("text", "before", "after", "says"),
    [
        ("ultracode off", "ultracode", "off", "Ultracode mode is off"),
        ("Ultracode OFF.", "ultracode", "off", "Ultracode mode is off"),
        ("turn off ultracode", "ultracode", "off", "Ultracode mode is off"),
        ("please turn ultracode off", "ultracode", "off", "Ultracode mode is off"),
        ("ultracode on", "off", "ultracode", "Ultracode mode is on"),
        ("ultracode mode on", "off", "ultracode", "Ultracode mode is on"),
        ("ultracode status", "ultracode", "ultracode", "Ultracode mode is on"),
        ("show ultracode status", "off", "off", "Ultracode mode is off"),
    ],
)
async def test_typed_mode_words_after_ultracode_set_or_show_the_mode_instead_of_running_the_pipeline(
    tmp_path, monkeypatch, text, before, after, says
):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    if before == "ultracode":
        await call(server, "slash.exec", {"command": "ultracode on"})
    await submit(server, text)
    assert pipe.calls == [] and models_called(server) == [] and progress(server) == []
    assert server.session.ultra_mode == after and ("ultra_mode" in server.session.stored.meta) == (after != "off")
    (user, reply) = transcript(server)
    assert user == ("user", text) and reply[0] == "assistant" and reply[1].startswith(says)
    assert events(server, "message.complete")[-1]["status"] == "done"
    assert all(e.get("text") != "running hooks" for e in events(server, "status.update"))  # none are configured


@pytest.mark.parametrize(
    ("text", "task"),
    [
        ("ultracode on the auth module", "on the auth module"),  # starts with a mode word, is still a task
        ("ultracode off-by-one in the pager", "off-by-one in the pager"),
        ("turn off the cache in ultracode", "turn off the cache in"),
        ("ultracode status page for the API", "status page for the API"),
    ],
)
async def test_a_task_that_merely_contains_a_mode_word_still_runs_the_pipeline(tmp_path, monkeypatch, text, task):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    await submit(server, text)
    assert pipe.calls == [("ultracode", task)] and server.session.ultra_mode == "off"


async def test_the_other_wake_words_have_no_mode_words(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    await submit(server, "ultraresearch off")
    assert pipe.calls == [("ultraresearch", "off")]


# ── the user's UserPromptSubmit hooks see a routed prompt first ──

BLOCK_HOOK = "  UserPromptSubmit:\n    - {command: \"echo 'has a secret' >&2; exit 2\"}\n"
SECRET = "my password is hunter2, fix the login form validation"


def _hooks(tmp_path, hooks_yaml: str) -> None:
    home = k3home(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("hooks:\n" + hooks_yaml, encoding="utf-8")


def _blocked(server: Any) -> bool:
    return "Prompt blocked by a UserPromptSubmit hook: has a secret" in [
        e.get("text", "") for e in events(server, "message.delta")
    ]


async def test_a_hook_that_blocks_the_prompt_stops_a_wake_word_run(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    _hooks(tmp_path, BLOCK_HOOK)
    await start(server, tmp_path)
    await submit(server, f"ultracode {SECRET}")
    assert pipe.calls == [] and models_called(server) == [] and progress(server) == []
    assert _blocked(server) and transcript(server) == []  # the prompt is not kept either
    done = events(server, "message.complete")[-1]
    assert done["status"] == "done" and "Prompt blocked" in done["text"]


async def test_a_hook_that_blocks_the_prompt_keeps_it_from_the_scope_classifier_and_the_mode_pipeline(
    tmp_path, monkeypatch
):
    server = make(tmp_path, monkeypatch, [verdict("small"), NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    _hooks(tmp_path, BLOCK_HOOK)
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, SECRET)
    assert models_called(server) == [] and classifier_calls(server) == 0  # not even the classifier saw it
    assert pipe.calls == [] and progress(server) == [] and transcript(server) == []
    assert _blocked(server)


async def test_a_blocked_bare_wake_word_gets_no_usage_line(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    _hooks(tmp_path, BLOCK_HOOK)
    await start(server, tmp_path)
    await submit(server, "ultracode")
    assert pipe.calls == [] and transcript(server) == [] and _blocked(server)


async def test_stop_while_a_hook_runs_ends_the_turn_before_any_job(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    _hooks(tmp_path, "  UserPromptSubmit: [{command: 'sleep 30'}]\n")
    await start(server, tmp_path)
    await call(server, "prompt.submit", {"text": "ultracode fix it"})
    task = server.session.turn_task
    for _ in range(500):  # until the hook is running
        if any(e.get("text") == "running hooks" for e in events(server, "status.update")):
            break
        await asyncio.sleep(0.01)
    assert (await call(server, "session.interrupt", {}))["interrupted"] is True
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and not server.session.turn_in_flight
    assert pipe.calls == [] and models_called(server) == [] and transcript(server) == []
    last = events(server, "status.update")[-1]
    assert last["text"] == "" and last["state"] == "completed"  # the busy line is cleared


async def test_a_hooks_context_goes_with_the_task_of_a_wake_word_job(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    _hooks(tmp_path, "  UserPromptSubmit: [{command: 'echo CTX-FROM-HOOK'}]\n")
    await start(server, tmp_path)
    await submit(server, "ultracode fix the login form")
    ((mode, task),) = pipe.calls
    assert mode == "ultracode" and task.startswith("fix the login form\n\n")
    assert "context from the user's hooks:" in task and task.endswith("CTX-FROM-HOOK\n```")
    # the transcript keeps what the user typed, and the status line names the job without the hook's text
    assert transcript(server)[0] == ("user", "ultracode fix the login form")
    assert all("CTX-FROM-HOOK" not in e.get("text", "") for e in events(server, "status.update"))
    shown = [e for e in events(server, "status.update") if e.get("text") == "running hooks"]
    assert [e["state"] for e in shown] == ["working"]  # the user sees the turn is busy while a hook runs


async def test_a_hooks_context_goes_with_the_task_of_a_mode_job(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("small"), NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    _hooks(tmp_path, "  UserPromptSubmit: [{command: 'echo CTX-FROM-HOOK'}]\n")
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "add a --verbose flag to the CLI")
    ((mode, task),) = pipe.calls
    assert mode == "ultracode" and task.startswith("add a --verbose flag to the CLI\n\n") and "CTX-FROM-HOOK" in task
    assert classifier_calls(server) == 1 and "CTX-FROM-HOOK" not in " ".join(
        c["text"] for p in server.providers for c in p.log
    )  # the classifier was asked about the prompt, not about the hook's text


async def test_the_hooks_run_once_per_typed_prompt_whether_or_not_it_is_routed(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("trivial"), NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    count = tmp_path / "hook-count.txt"
    _hooks(
        tmp_path,
        f"  SessionStart: [{{command: 'echo start >> {count}'}}]\n"
        f"  UserPromptSubmit: [{{command: 'echo prompt >> {count}'}}]\n",
    )
    await start(server, tmp_path)
    server.session.ultra_mode = "ultracode"
    await submit(server, "what does the retry flag do?")  # classified trivial: falls through to a normal turn
    assert pipe.calls == [] and transcript(server)[-1] == ("assistant", "NORMAL-TURN")
    assert count.read_text().split() == ["start", "prompt"]
    await submit(server, "ultracode fix the build")  # a wake word job
    assert pipe.calls == [("ultracode", "fix the build")]
    assert count.read_text().split() == ["start", "prompt", "prompt"]  # SessionStart only the first time
    server.session.ultra_mode = "off"
    await submit(server, "and a plain prompt")
    assert count.read_text().split() == ["start", "prompt", "prompt", "prompt"]


# ── config ──


def test_wake_config_defaults_and_helpers():
    assert Settings().wake_words == {"enabled": True, "ultracode": True, "ultraplan": True, "ultraresearch": True}
    assert wake_cfg(Settings(wake_words={"ultraplan": False})) == {
        "enabled": True,
        "ultracode": True,
        "ultraplan": False,
        "ultraresearch": True,
    }
    hit = detect_enabled(Settings(), "ultracode fix x")
    assert hit is not None and hit.mode == "ultracode"
    assert detect_enabled(Settings(wake_words={"enabled": False}), "ultracode fix x") is None
    assert detect_enabled(Settings(wake_words={"ultracode": False}), "ultracode fix x") is None
    other = detect_enabled(Settings(wake_words={"ultracode": False}), "ultraplan fix x")
    assert other is not None and other.mode == "ultraplan"
    assert wake_cfg(SimpleNamespace(wake_words={"enabled": "no"}))["enabled"] is True  # not a bool: the default


def test_wake_words_and_min_scope_are_validated():
    with pytest.raises(ValidationError, match=r"wake_words.enabled must be true or false"):
        Settings(wake_words={"enabled": "off"})
    with pytest.raises(ValidationError, match=r"ultracode.min_scope must be one of trivial, small"):
        Settings(ultracode={"min_scope": "gigantic"})
    assert Settings(ultracode={"min_scope": " Large "}).ultracode["min_scope"] == "large"
    assert Settings(ultracode={"max_agents": 3}).ultracode == {"max_agents": 3}


def test_unknown_keys_under_ultracode_and_wake_words_are_warned_about(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        json.dumps({"ultracode": {"min_scope": "medium", "max_agent": 3}, "wake_words": {"ultraplan": False, "x": 1}})
    )
    with caplog.at_level(logging.WARNING, logger="k3code.config"):
        cfg = load_config()
    assert cfg.ultracode["min_scope"] == "medium" and cfg.wake_words["ultraplan"] is False
    assert cfg.wake_words["ultracode"] is True  # the merge replaced the section; the others stay on
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ultracode.max_agent is not used" in text and "wake_words.x is not used" in text
    assert "min_scope is not used" not in text and "ultraplan is not used" not in text


LOG = "Traceback: ultraresearch.py line 3 failed"  # a pasted log that names another wake word


async def test_a_wake_word_in_pasted_text_never_runs_a_mode_now_queued_or_steered(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    pipe = pipeline(server, monkeypatch)
    await start(server, tmp_path)
    text = f"why does this fail?\nultraresearch failed {LOG}"
    paste = [[20, len(text)]]
    await submit(server, text, paste_spans=paste)  # the only wake word is pasted: a normal turn
    assert pipe.calls == [] and transcript(server)[-1] == ("assistant", "NORMAL-TURN")
    # typed "ultracode" plus a pasted "ultraresearch": without the spans that is two modes, which is no wake word
    typed = f"ultracode fix this: {LOG}"
    await submit(server, typed, paste_spans=[[20, len(typed)]])
    assert pipe.calls == [("ultracode", f"fix this: {LOG}")]
    # queued behind a turn and steered into one, the spans travel with the prompt
    pipe.hold = asyncio.Event()
    pipe.started.clear()
    await call(server, "prompt.submit", {"text": "ultracode build the thing"})
    task = server.session.turn_task
    await pipe.started.wait()
    queued = await call(server, "prompt.submit", {"text": typed, "paste_spans": [[20, len(typed)]]})
    assert queued["status"] == "queued"
    steer = f"ultraplan and this: {LOG}"
    assert (await call(server, "session.steer", {"text": steer, "paste_spans": [[20, len(steer)]]}))["steered"]
    pipe.hold.set()
    await asyncio.wait_for(task, 20)
    await server.autonomy.drain()
    assert pipe.calls[1:] == [("ultracode", "build the thing"), ("ultracode", f"fix this: {LOG}")]
    steered = transcript(server)[-3]  # ultraplan is not the faked pipeline: it ran (and found no planner)
    assert steered[0] == "assistant" and steered[1].startswith(f"/ultraplan and this: {LOG} failed")


@pytest.mark.parametrize("spans", [[[0]], [[3, 1]], [[0, 999]], [[-1, 2]], [[True, 2]], "0-5", [["0", "5"]]])
async def test_malformed_paste_spans_are_refused(tmp_path, monkeypatch, spans):
    server = make(tmp_path, monkeypatch, [NORMAL], autonomy=NO_GATE)
    await start(server, tmp_path)
    for method in ("prompt.submit", "session.steer"):
        n = len(server._frames)
        msg = {"jsonrpc": "2.0", "id": 9, "method": method, "params": {"text": "ultracode x", "paste_spans": spans}}
        await server._handle_line(json.dumps(msg))
        out = [json.loads(x) for x in server._frames[n:] if json.loads(x).get("id") == 9]
        assert "paste_spans" in out[0]["error"]["message"]
