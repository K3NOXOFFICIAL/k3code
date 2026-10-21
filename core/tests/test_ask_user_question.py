"""ask_user_question: the model asks the user 1-4 multiple-choice questions through the TUI's clarify overlay."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from k3code import ask_user
from k3code.ask_user import NAME, decode_answers, parse_questions
from k3code.gateway.server import LiveSession
from k3code.permissions import decide
from k3code.tools import build_registry
from test_autonomy_gateway import call, events, make, start
from test_subagent_prompt_tools import spawn

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}


def q(question="Which db?", header="Database", labels=("Postgres", "SQLite"), multi=False) -> dict:
    return {
        "question": question,
        "header": header,
        "options": [{"label": label, "description": f"use {label}"} for label in labels],
        "multiSelect": multi,
    }


TWO = [q(), q("Which extras?", "Extras", ("Auth", "Cache", "Metrics"), multi=True)]


def frames(server) -> list[dict]:
    return [json.loads(x) for x in server._frames]


async def turn(server, text: str, answer) -> list[dict]:
    """Run one turn, answering each clarify request with ``answer(params)`` (``None``: leave it unanswered)."""
    await call(server, "prompt.submit", {"text": text})
    task = server.session.turn_task
    seen: list[dict] = []
    for _ in range(1000):
        for f in frames(server):
            if f.get("method") == "clarify" and f["id"] not in {s["id"] for s in seen}:
                seen.append(f)
                reply = answer(f["params"])
                if reply is not None:
                    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": f["id"], **reply}))
        if task.done():
            break
        await asyncio.sleep(0.01)
    await task
    return seen


def _parsed(text: str) -> dict:
    """A result as the model reads it: an error as "Error: <text>", anything else as JSON."""
    return {"error": text.removeprefix("Error: ")} if text.startswith("Error: ") else json.loads(text)


def results(server) -> list[dict]:
    """What the model was handed for each call."""
    return [_parsed(e["result_text"]) for e in events(server, "tool.complete") if e["name"] == NAME]


async def gateway(tmp_path, monkeypatch, questions, *, calls: int = 1, mode: str = "default"):
    steps = [
        *(
            {"type": "tool_call", "when": "first", "id": f"c{n}", "name": NAME, "arguments": {"questions": questions}}
            for n in range(calls)
        ),
        {"type": "text", "when": "after_tool", "text": "done"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode=mode, **NO_GATE)
    server.approval_timeout_s = 5.0  # a broken build fails in seconds, not after the 30 min default
    await start(server, tmp_path)
    return server


# ── the frame and the answers, end to end ──


async def test_frame_carries_every_question_and_the_answers_reach_the_model(tmp_path, monkeypatch) -> None:
    server = await gateway(tmp_path, monkeypatch, TWO)
    answers = {"q1": "Postgres", "q2": json.dumps(["Auth", "a custom extra"])}
    (frame,) = await turn(server, "set it up", lambda p: {"result": {"answers": answers}})
    params = frame["params"]
    assert params["session_id"] == server.session.session_id
    assert params["question"] == "Which db?"  # the summary panes.py and the slash daemon read
    assert params["questions"] == [
        {
            "qid": "q1",
            "question": "Which db?",
            "header": "Database",
            "choices": ["Postgres", "SQLite"],
            "descriptions": ["use Postgres", "use SQLite"],
            "multi_select": False,
        },
        {
            "qid": "q2",
            "question": "Which extras?",
            "header": "Extras",
            "choices": ["Auth", "Cache", "Metrics"],
            "descriptions": ["use Auth", "use Cache", "use Metrics"],
            "multi_select": True,
        },
    ]
    assert results(server) == [
        {
            "answers": [
                {"header": "Database", "question": "Which db?", "answer": "Postgres", "custom": False},
                {
                    "header": "Extras",
                    "question": "Which extras?",
                    "answers": ["Auth", "a custom extra"],
                    "custom": True,
                },
            ]
        }
    ]
    # never approval-prompted, even in the default (ask) mode
    assert not [f for f in frames(server) if f.get("method") == "approval"]


async def test_unanswered_questions_time_out_with_their_own_wording(tmp_path, monkeypatch) -> None:
    server = await gateway(tmp_path, monkeypatch, [q()])
    server.approval_timeout_s = 0.05
    await turn(server, "set it up", lambda p: None)
    (result,) = results(server)
    assert "did not answer" in result["error"] and "timed out" in result["error"]
    assert any(f.get("method") == "request.cancel" for f in frames(server))


async def test_a_client_that_cannot_show_questions_is_not_a_timeout(tmp_path, monkeypatch) -> None:
    server = await gateway(tmp_path, monkeypatch, [q()])
    await turn(server, "set it up", lambda p: {"error": {"code": -32000, "message": "cannot answer here"}})
    (result,) = results(server)
    assert "could not be shown: cannot answer here" in result["error"] and "timed out" not in result["error"]


async def test_unattended_is_refused_at_call_time_without_asking(tmp_path, monkeypatch) -> None:
    # two calls in one turn: the session goes unattended (Ctrl+B) after the loop was built, between the calls
    server = await gateway(tmp_path, monkeypatch, [q()], calls=2)

    def answer(params):
        server.session.background = True
        return {"result": {"answers": {"q1": "SQLite"}}}

    seen = await turn(server, "set it up", answer)
    assert len(seen) == 1  # the second call never reached the client
    first, second = results(server)
    assert first["answers"][0]["answer"] == "SQLite"
    assert second == {"error": ask_user.UNATTENDED}


async def test_interrupting_the_turn_cancels_the_open_request_and_is_not_a_tool_result(tmp_path, monkeypatch) -> None:
    server = await gateway(tmp_path, monkeypatch, [q()])
    await call(server, "prompt.submit", {"text": "set it up"})
    task = server.session.turn_task
    for _ in range(1000):  # until the clarify request is open
        if any(f.get("method") == "clarify" for f in frames(server)):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("no clarify request was sent")
    assert (await call(server, "session.interrupt", {}))["interrupted"] is True
    with pytest.raises(asyncio.CancelledError):
        await task
    (cancel,) = [f for f in frames(server) if f.get("method") == "request.cancel"]
    assert cancel["params"]["reason"] == "interrupted" and cancel["params"]["method"] == "clarify"
    assert task.cancelled() and server.session.user_stopped  # ended interrupted: a cancelled turn, not a finished one
    assert not server.session.streaming and not events(server, "message.complete")
    assert results(server) == []  # the handler let the cancellation through: nothing answered the model


async def test_two_calls_in_one_reply_never_run_concurrently(tmp_path, monkeypatch) -> None:
    server = await gateway(tmp_path, monkeypatch, [q()], calls=2)
    open_at_answer: list[int] = []

    def answer(params):
        open_at_answer.append(len(server._open_requests))  # requests open while this one is answered
        return {"result": {"answers": {"q1": "SQLite"}}}

    seen = await turn(server, "set it up", answer)
    assert len(seen) == 2 and open_at_answer == [1, 1]  # the second request opens only after the first was answered


@pytest.mark.parametrize(
    "flags", [{"background": True}, {"task_kind": "cron_job"}, {"task_kind": "loop_tick"}, {"goal_continuation": True}]
)
def test_every_unattended_kind_counts(flags) -> None:
    base = {"background": False, "task_kind": "", "goal_continuation": False}
    unattended = LiveSession.unattended.fget  # the one predicate, read off the property so no live session is needed
    assert unattended(SimpleNamespace(**{**base, **flags}))
    assert not unattended(SimpleNamespace(**base))


# ── permissions and where the tool exists ──


@pytest.mark.parametrize("mode", ["default", "plan", "accept-edits", "auto"])
def test_never_prompted_and_allowed_in_plan_mode(mode) -> None:
    d = decide(mode=mode, tool=NAME, args={"questions": TWO}, cwd="/proj")
    assert d.action == "allow", d


def test_absent_from_the_headless_registry() -> None:
    assert NAME not in build_registry().names()


async def test_main_loop_has_it_and_a_subagent_does_not(tmp_path, monkeypatch) -> None:
    server, child = await spawn(tmp_path, monkeypatch, "worker")
    assert NAME not in child.tools.names()
    calls = [c for p in server.providers for c in p.log]
    parent = [c for c in calls if "PARENT: delegate it" in c["text"] and "[agent:" not in c["text"]]
    assert parent and all(NAME in c["tools"] for c in parent)
    children = [c for c in calls if "[agent:worker]" in c["text"]]
    assert children and all(NAME not in c["tools"] for c in children)


# ── decoding ──


def questions(*qs: dict) -> list[dict]:
    out = parse_questions({"questions": list(qs)})
    assert isinstance(out, list), out
    return out


def test_single_select_label_and_typed_text() -> None:
    qs = questions(q())
    assert decode_answers(qs, {"answers": {"q1": "SQLite"}}) == {
        "answers": [{"header": "Database", "question": "Which db?", "answer": "SQLite", "custom": False}]
    }
    typed = decode_answers(qs, {"answers": {"q1": "DuckDB please"}})
    assert typed["answers"][0]["answer"] == "DuckDB please" and typed["answers"][0]["custom"] is True


def test_a_single_select_answer_that_looks_like_json_stays_a_string() -> None:
    out = decode_answers(questions(q()), {"answers": {"q1": '["x"]'}})
    assert out["answers"][0]["answer"] == '["x"]' and "answers" not in out["answers"][0]
    assert out["answers"][0]["custom"] is True


def test_multi_select_decodes_its_array_and_falls_back_to_typed_text() -> None:
    qs = questions(q(labels=("A", "B", "C"), multi=True))
    picked = decode_answers(qs, {"answers": {"q1": '["A", "C"]'}})["answers"][0]
    assert picked["answers"] == ["A", "C"] and picked["custom"] is False
    typed = decode_answers(qs, {"answers": {"q1": "not json [A"}})["answers"][0]
    assert typed["answers"] == ["not json [A"] and typed["custom"] is True
    mixed = decode_answers(qs, {"answers": {"q1": '["A", 3]'}})["answers"][0]  # not a list of strings
    assert mixed["answers"] == ['["A", 3]'] and mixed["custom"] is True


def test_chat_returns_the_partial_answers_and_tells_the_model_to_stop_asking() -> None:
    qs = questions(*TWO)
    out = decode_answers(qs, {"answers": {"q1": "Postgres"}, "chat": True})
    assert out["user_chose_to_chat"] is True
    assert [a["question"] for a in out["answers"]] == ["Which db?"]
    assert out["instruction"] == ask_user.CHAT_INSTRUCTION
    assert decode_answers(qs, {"chat": True})["user_chose_to_chat"] is True  # chat with nothing answered is no cancel


@pytest.mark.parametrize("result", [{}, {"answer": ""}, {"answers": {}}, {"answers": {"q1": "  "}}])
def test_cancel_is_a_dismissal(result) -> None:
    assert decode_answers(questions(q()), result) == {"error": "User dismissed the questions"}


def test_a_lone_legacy_answer_answers_the_first_question() -> None:
    out = decode_answers(questions(*TWO), {"answer": "Postgres"})
    assert out == {
        "answers": [{"header": "Database", "question": "Which db?", "answer": "Postgres", "custom": False}],
        "unanswered": ["Extras"],
    }


# ── validation ──


@pytest.mark.parametrize(
    ("qs", "needle"),
    [
        ([], "1-4 questions"),
        ([q()] * 5, "1-4 questions"),
        (["text"], "must be an object"),
        ([q(question=" ")], "no question text"),
        ([q(header="")], "no header"),
        ([q(header="Much too long header")], "use at most 12"),
        ([q(labels=("Only",))], "2-4 options"),
        ([q(labels=("A", "B", "C", "D", "E"))], "2-4 options"),
        ([q(labels=("A", " "))], "non-empty label"),
        ([q(labels=("A", "A "))], "appears twice"),
    ],
)
async def test_invalid_questions_are_rejected_before_anyone_is_asked(qs, needle) -> None:
    asked: list = []
    server = SimpleNamespace(_ask_client=lambda *a: asked.append(a), approval_timeout_s=5.0)
    session = SimpleNamespace(unattended=False, session_id="s")
    reg = SimpleNamespace(register=lambda spec, handler: setattr(reg, "handler", handler))
    ask_user.register(reg, server, session)
    out = await reg.handler({"questions": qs})
    assert needle in out["error"] and not asked


def test_a_header_of_twelve_characters_is_accepted() -> None:
    assert questions(q(header="Twelve chars"))[0]["header"] == "Twelve chars"


# ── input hardening ──


def rejected(**kw) -> str:
    out = parse_questions({"questions": [q(**kw)]})
    assert isinstance(out, str), out
    return out


@pytest.mark.parametrize(
    "bad", ["\x1b[31m", "a\x07b", "a\x00b", "tab\there", "a\x7fb", "a\x85b", "a\u202eb", "a\u2066b"]
)
def test_control_and_bidi_characters_are_rejected_in_every_field(bad) -> None:
    assert "question 1: question contains a control character" in rejected(question=f"Which {bad}?")
    assert "question 1: header contains a control character" in rejected(header=f"H{bad}")
    assert "question 1: option label contains a control character" in rejected(labels=(f"A{bad}", "B"))
    opts = [{"label": "A", "description": f"d{bad}"}, {"label": "B", "description": ""}]
    out = parse_questions({"questions": [{**q(), "options": opts}]})
    assert isinstance(out, str) and "question 1: option description contains a control character" in out


def test_esc_in_a_label_is_rejected() -> None:
    assert "option label contains a control character (U+001B)" in rejected(labels=("\x1b[2JPostgres", "SQLite"))


def test_a_newline_is_allowed_in_question_and_description_only() -> None:
    opts = [{"label": "A", "description": "line one\nline two"}, {"label": "B", "description": ""}]
    out = parse_questions({"questions": [{**q(question="Which?\nBe brief."), "options": opts}]})
    assert isinstance(out, list) and out[0]["question"] == "Which?\nBe brief."
    assert out[0]["descriptions"][0] == "line one\nline two"
    assert "option label contains a control character" in rejected(labels=("A\nB", "C"))
    assert "header contains a control character" in rejected(header="A\nB")


def test_length_caps_in_characters() -> None:
    assert "question 1: question is 1001 characters; use at most 1000" in rejected(question="x" * 1001)
    assert "question 1: option label is 81 characters; use at most 80" in rejected(labels=("x" * 81, "B"))
    opts = [{"label": "A", "description": "x" * 301}, {"label": "B", "description": ""}]
    out = parse_questions({"questions": [{**q(), "options": opts}]})
    assert isinstance(out, str) and "question 1: option description is 301 characters; use at most 300" in out
    at_cap = [{"label": "x" * 80, "description": "y" * 300}, {"label": "B", "description": ""}]
    assert isinstance(parse_questions({"questions": [{**q(question="z" * 1000), "options": at_cap}]}), list)


def test_the_error_names_the_question_number() -> None:
    out = parse_questions({"questions": [q(), q(question="x" * 1001)]})
    assert isinstance(out, str) and out.startswith("question 2:")


def test_header_counts_display_cells() -> None:
    assert "14 display cells" in rejected(header="データベース設")  # 7 characters, 2 cells each
    assert questions(q(header="データベース"))[0]["header"] == "データベース"  # 6 wide characters: exactly 12 cells
    assert questions(q(header="ABCDEFGHIJKL"))[0]["header"] == "ABCDEFGHIJKL"  # 12 ASCII
    assert "13 display cells" in rejected(header="ABCDEFGHIJKLM")
    assert questions(q(header="e\u0301" * 12))  # combining marks take no cell


@pytest.mark.parametrize("bad", ["true", 1, 0, None, "false", []])
def test_multi_select_that_is_not_a_bool_is_an_error(bad) -> None:
    out = parse_questions({"questions": [{**q(), "multiSelect": bad}]})
    assert isinstance(out, str) and out.startswith("question 1: multiSelect must be true or false")


def test_multi_select_absent_means_single_select() -> None:
    raw = q()
    del raw["multiSelect"]
    assert questions(raw)[0]["multi_select"] is False


def test_unanswered_questions_are_listed_by_header() -> None:
    qs = questions(*TWO, q("Which os?", "OS"))
    out = decode_answers(qs, {"answers": {"q2": json.dumps(["Auth"])}})
    assert out["unanswered"] == ["Database", "OS"]
    lone = decode_answers(qs, {"answer": "Postgres"})  # an older TUI answers q1 only
    assert lone["unanswered"] == ["Extras", "OS"]
    chat = decode_answers(qs, {"answers": {"q1": "Postgres"}, "chat": True})
    assert chat["unanswered"] == ["Extras", "OS"] and chat["user_chose_to_chat"] is True
    empty_pick = decode_answers(qs, {"answers": {"q1": "Postgres", "q2": "[]"}})
    assert empty_pick["unanswered"] == ["Extras", "OS"]


def test_no_unanswered_key_when_everything_was_answered() -> None:
    assert "unanswered" not in decode_answers(questions(q()), {"answers": {"q1": "SQLite"}})
