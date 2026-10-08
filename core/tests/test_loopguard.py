"""Doom-loop guard: repeated tool calls / repeated assistant messages."""

from __future__ import annotations

from k3code.reliability.loopguard import LoopGuard, Verdict, normalize_args


def test_normalize_args_numeric_equivalence():
    assert normalize_args({"a": 30}) == normalize_args({"a": 30.0})


def test_normalize_args_string_rstrip():
    assert normalize_args({"a": "x\n"}) == normalize_args({"a": "x"})


def test_tool_call_below_threshold_is_ok():
    guard = LoopGuard()
    for _ in range(2):
        outcome = guard.observe_tool_call("bash", {"cmd": "ls"})
        assert outcome.verdict is Verdict.OK


def test_tool_call_third_repeat_triggers_note():
    guard = LoopGuard()
    guard.observe_tool_call("bash", {"cmd": "ls"})
    guard.observe_tool_call("bash", {"cmd": "ls"})
    outcome = guard.observe_tool_call("bash", {"cmd": "ls"})
    assert outcome.verdict is Verdict.NOTE
    assert "bash" in outcome.note
    assert not guard.needs_input


def test_tool_call_repeat_after_note_stops_turn():
    guard = LoopGuard()
    guard.observe_tool_call("bash", {"cmd": "ls"})
    guard.observe_tool_call("bash", {"cmd": "ls"})
    guard.observe_tool_call("bash", {"cmd": "ls"})  # NOTE
    outcome = guard.observe_tool_call("bash", {"cmd": "ls"})  # STOP
    assert outcome.verdict is Verdict.STOP
    assert guard.needs_input


def test_different_args_reset_the_run():
    guard = LoopGuard()
    guard.observe_tool_call("bash", {"cmd": "ls"})
    guard.observe_tool_call("bash", {"cmd": "ls"})
    outcome = guard.observe_tool_call("bash", {"cmd": "pwd"})
    assert outcome.verdict is Verdict.OK


def test_tool_call_resets_text_run_and_vice_versa():
    guard = LoopGuard()
    guard.observe_message("same message")
    guard.observe_tool_call("bash", {"cmd": "ls"})  # interleaved: breaks the text run
    outcome = guard.observe_message("same message")
    assert outcome.verdict is Verdict.OK  # run restarted at 1, not 2


def test_identical_assistant_message_triggers_note_then_stop():
    guard = LoopGuard()
    outcome1 = guard.observe_message("I'm stuck.")
    assert outcome1.verdict is Verdict.OK
    outcome2 = guard.observe_message("I'm stuck.")  # 2nd in a row -> NOTE
    assert outcome2.verdict is Verdict.NOTE
    outcome3 = guard.observe_message("I'm stuck.")  # 3rd -> STOP
    assert outcome3.verdict is Verdict.STOP
    assert guard.needs_input


def test_message_none_is_ok_and_does_not_affect_state():
    guard = LoopGuard()
    guard.observe_message("hello")
    outcome = guard.observe_message(None)
    assert outcome.verdict is Verdict.OK


def test_reset_clears_state():
    guard = LoopGuard()
    guard.observe_tool_call("bash", {"cmd": "ls"})
    guard.observe_tool_call("bash", {"cmd": "ls"})
    guard.observe_tool_call("bash", {"cmd": "ls"})  # NOTE
    guard.reset()
    outcome = guard.observe_tool_call("bash", {"cmd": "ls"})
    assert outcome.verdict is Verdict.OK
    assert not guard.needs_input
