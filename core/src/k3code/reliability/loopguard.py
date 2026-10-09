"""Doom-loop guard: detect stuck repetitions and break them.

Triggers:
- the same tool + normalized args called 3+ times in a row;
- an identical assistant message (without tool calls) repeated 2+ times;
- on results (a reminder only, once per pattern and turn): the same failure signature 3 tool calls in a row, whatever
  their args, and ABABAB alternation of two calls (with their outcomes) over the last 6 calls.

Behavior on first trigger: inject a corrective system note telling the model
what it is repeating and to try a different approach (once). On a second
consecutive trigger: stop the turn and mark the session ``needs_input``.

Arg normalization: JSON-dump with sorted keys; strings are stripped of
trailing whitespace; numeric-like values are compared by canonical JSON
encoding so ``30`` and ``30.0`` count as the same.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

TOOL_REPEATS = 3  # same tool+args N times in a row
TEXT_REPEATS = 2  # identical assistant text N times in a row

CORRECTIVE_NOTE = (
    "[loop guard] You have repeated the same {what} {n} times in a row. "
    "Stop and reconsider: try a different approach, inspect the result of the "
    "last call (it may already contain the answer), or ask the user for help. "
    "Repeating it again will end the turn."
)

FAILURE_REPEATS = 3  # the same failure signature N tool calls in a row (the args may differ)
ALTERNATION_WINDOW = 6  # ABABAB over the last N calls (call and outcome alike)

SAME_FAILURE_NOTE = (
    "[loop guard] The last {n} tool calls failed the same way. Last call: {call}. Error: {error}. "
    "Trying it again, or a small variation of it, will fail again: take a different approach (another command or "
    "tool, fix what the error names first, check the arguments), or ask the user."
)
ALTERNATION_NOTE = (
    "[loop guard] You are alternating between the same two tool calls ({n} times each, with the same results "
    "each time). That is a loop: use what those results already tell you and change approach, or ask the user."
)


def normalize_args(args: Any) -> str:
    """Canonical fingerprint of tool-call arguments for repetition detection."""
    return json.dumps(_normalize(args), sort_keys=True, default=str)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


class Verdict(Enum):
    OK = "ok"  # nothing suspicious
    NOTE = "note"  # first trigger: inject a corrective system note
    STOP = "stop"  # second trigger: stop the turn, mark needs_input


@dataclass
class GuardOutcome:
    verdict: Verdict
    note: str = ""  # corrective note for NOTE verdicts
    key: str = ""  # fingerprint of what repeated


@dataclass
class LoopGuard:
    """Stateful repetition detector for one session/turn."""

    tool_repeats: int = TOOL_REPEATS
    text_repeats: int = TEXT_REPEATS
    failure_repeats: int = FAILURE_REPEATS

    def __post_init__(self) -> None:
        self._last_tool_key: str | None = None
        self._tool_run = 0
        self._last_text_key: str | None = None
        self._text_run = 0
        self._note_injected = False  # one note per repeated pattern
        self._note_key: str | None = None
        self.needs_input = False
        self._reset_results()

    def _reset_results(self) -> None:
        self._recent: deque[str] = deque(maxlen=ALTERNATION_WINDOW)  # call+outcome keys, newest last
        self._fail_key: str | None = None
        self._fail_run = 0
        self._result_notes: set[str] = set()  # result patterns already reminded about this turn

    def reset(self) -> None:
        """Start a fresh turn."""
        self._last_tool_key = None
        self._tool_run = 0
        self._last_text_key = None
        self._text_run = 0
        self._note_injected = False
        self._note_key = None
        self.needs_input = False
        self._reset_results()

    def observe_tool_call(self, tool: str, args: Any) -> GuardOutcome:
        """Record a tool call; detect same-tool+args runs."""
        key = f"tool:{tool}:{normalize_args(args)}"
        if key == self._last_tool_key:
            self._tool_run += 1
        else:
            self._last_tool_key = key
            self._tool_run = 1
            self._note_injected = False
            self._note_key = None
        # A tool call breaks any text run.
        self._last_text_key = None
        self._text_run = 0
        return self._verdict(key, self._tool_run, self.tool_repeats, what=f"tool call `{tool}`")

    def observe_tool_result(self, tool: str, args: Any, failure: str | None, call: str = "") -> GuardOutcome:
        """Record a tool call's outcome (``failure``: its error signature, None on success); NOTE once per pattern.

        Requests alone miss loops whose calls differ: the same failing command with tweaked args, and ABAB
        alternation between two calls. ``call`` names the call in the reminder (``bash `npm test` ``).
        """
        key = f"{tool}:{normalize_args(args)}->{failure or 'ok'}"
        self._recent.append(key)
        if failure is None:
            self._fail_key, self._fail_run = None, 0
        else:
            fail_key = f"{tool}:{failure}"
            self._fail_run = self._fail_run + 1 if fail_key == self._fail_key else 1
            self._fail_key = fail_key
            if self._fail_run >= self.failure_repeats and fail_key not in self._result_notes:
                self._result_notes.add(fail_key)
                return GuardOutcome(
                    Verdict.NOTE,
                    note=SAME_FAILURE_NOTE.format(n=self._fail_run, call=call or tool, error=failure),
                    key=f"fail:{fail_key}",
                )
        r = list(self._recent)
        if len(r) == ALTERNATION_WINDOW and r[0] != r[1] and all(r[i] == r[i % 2] for i in range(len(r))):
            pair = "|".join(sorted(r[:2]))
            if pair not in self._result_notes:
                self._result_notes.add(pair)
                return GuardOutcome(Verdict.NOTE, note=ALTERNATION_NOTE.format(n=len(r) // 2), key=f"abab:{pair}")
        return GuardOutcome(Verdict.OK, key=key)

    def observe_message(self, content: str | None) -> GuardOutcome:
        """Record an assistant message without tool calls (or None → skip)."""
        if content is None:
            return GuardOutcome(Verdict.OK)
        key = f"text:{content.rstrip()}"
        if key == self._last_text_key:
            self._text_run += 1
        else:
            self._last_text_key = key
            self._text_run = 1
            self._note_injected = False
            self._note_key = None
        # A text message resets the tool run (interleaving means not "in a row").
        self._last_tool_key = None
        self._tool_run = 0
        return self._verdict(key, self._text_run, self.text_repeats, what="message")

    def _verdict(self, key: str, run: int, threshold: int, *, what: str) -> GuardOutcome:
        if run < threshold:
            return GuardOutcome(Verdict.OK, key=key)
        if self._note_key != key:
            # Fresh pattern: first trigger → corrective note.
            self._note_key = key
            self._note_injected = True
            return GuardOutcome(
                Verdict.NOTE,
                note=CORRECTIVE_NOTE.format(what=what, n=run),
                key=key,
            )
        if self._note_injected:
            # Already warned for this pattern → stop the turn.
            self.needs_input = True
            return GuardOutcome(
                Verdict.STOP,
                note=f"[loop guard] {what} repeated again after the warning; stopping the turn.",
                key=key,
            )
        self._note_injected = True
        return GuardOutcome(Verdict.NOTE, note=CORRECTIVE_NOTE.format(what=what, n=run), key=key)
