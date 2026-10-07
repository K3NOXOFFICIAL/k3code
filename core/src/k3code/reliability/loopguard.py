"""Doom-loop guard: detect stuck repetitions and break them.

Triggers:
- the same tool + normalized args called 3+ times in a row;
- an identical assistant message (without tool calls) repeated 2+ times.

Behavior on first trigger: inject a corrective system note telling the model
what it is repeating and to try a different approach (once). On a second
consecutive trigger: stop the turn and mark the session ``needs_input``.

Arg normalization: JSON-dump with sorted keys; strings are stripped of
trailing whitespace; numeric-like values are compared by canonical JSON
encoding so ``30`` and ``30.0`` count as the same.
"""

from __future__ import annotations

import json
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

    def __post_init__(self) -> None:
        self._last_tool_key: str | None = None
        self._tool_run = 0
        self._last_text_key: str | None = None
        self._text_run = 0
        self._note_injected = False  # one note per repeated pattern
        self._note_key: str | None = None
        self.needs_input = False

    def reset(self) -> None:
        """Start a fresh turn."""
        self._last_tool_key = None
        self._tool_run = 0
        self._last_text_key = None
        self._text_run = 0
        self._note_injected = False
        self._note_key = None
        self.needs_input = False

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
        return GuardOutcome(
            Verdict.NOTE, note=CORRECTIVE_NOTE.format(what=what, n=run), key=key
        )
