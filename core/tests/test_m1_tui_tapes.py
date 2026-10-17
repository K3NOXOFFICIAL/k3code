"""The M1 TUI tapes (scripts/exit/m1_tui.py) wait for each turn's own answer (#170).

The permissions tape ran four turns that all ended with the same answer, "turn-complete". That text stays on screen
from the first turn, so from the second turn on every ``wait("turn-complete")`` returned at once, before the turn had
run its tool: "always: not run" whenever the gateway took longer than the one-second pause before the wait (a loaded
machine). Here a stand-in for the TUI and its gateway plays the tape with a slow tool run, so the race is
deterministic: the tape passes only if it waits for something the turn itself produces.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3code.providers.fake import FakeProvider
from k3code.providers.types import Message, ToolCall

REPO = Path(__file__).resolve().parents[2]
EXIT = REPO / "scripts" / "exit"

pytestmark = pytest.mark.skipif(not (EXIT / "m1_tui.py").is_file(), reason="scripts/exit is not part of the export")

#: pumps of 0.2 s between an approval answer and the end of the turn: the tool run on a busy machine (1.6 s), longer
#: than the tape's one-second pause after the answer
LATENCY = 8
CHOICES = {"1": "once", "2": "session", "3": "always", "4": "deny"}


@pytest.fixture
def m1(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(EXIT))
    spec = importlib.util.spec_from_file_location("m1_tui_under_test", EXIT / "m1_tui.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "TMP", tmp_path)
    return mod


def _sim_tui(base: type) -> type:
    """A subclass of the harness's ``Tui`` that plays the TUI, the gateway and the scripted fake provider for one
    bash-calling tape. Only the screen and the clock are simulated; ``wait``, ``wait_gone``, ``mark`` and
    ``ever_text`` are the harness's own."""

    class SimTui(base):
        def __init__(self, home: Path, cwd: Path, **_kw) -> None:
            self.home, self.cwd = home, cwd
            cwd.mkdir(parents=True, exist_ok=True)
            self.lines: list[str] = ["⌘ k3code", "❯ "]
            self.ever, self._seen = [], set()
            self.provider = FakeProvider(steps=json.loads((home / "fake.json").read_text()))
            self.messages: list[Message] = []
            self.allowed: set[str] = set()  # commands with an always/session rule
            self.asking: str | None = None  # the command an open approval asks about
            self.events: list[tuple[int, object]] = []
            self.tick = 0
            self.child = SimpleNamespace(send=self._send)

        # ── screen and clock ──
        def text(self) -> str:
            return "\n".join(self.lines)

        def _record(self) -> None:
            for s in self.lines:
                if s.strip() and s not in self._seen:
                    self._seen.add(s)
                    self.ever.append(s)

        def pump(self, sec: float = 0.5) -> None:
            for _ in range(max(1, round(sec / 0.2))):
                self.tick += 1
                due = [e for e in self.events if e[0] <= self.tick]
                self.events = [e for e in self.events if e[0] > self.tick]
                for _, fn in due:
                    fn()
                self._record()

        def later(self, ticks: int, fn) -> None:
            self.events.append((self.tick + ticks, fn))

        # ── input ──
        def line(self, text: str, settle: float = 0.6) -> None:
            self.lines.append(f"❯ {text}")
            self.messages.append(Message(role="user", content=text))
            reply = self._call()
            call = reply.tool_calls[0]
            self.messages.append(Message(role="assistant", content=None, tool_calls=[call]))
            command = call.arguments["command"]
            if command in self.allowed:
                self.later(LATENCY, lambda: self._finish(call, True))
            else:
                self.later(1, lambda: self._ask(command, call))
            self.pump(settle + 0.4)

        def key(self, name: str) -> None:
            if name == "enter":
                self._answer("once")
            self.pump(0.3)

        def _send(self, ch: str) -> None:
            if ch in CHOICES:
                self._answer(CHOICES[ch])

        def close(self) -> None:
            pass

        # ── the gateway ──
        def _call(self) -> Message:
            async def run() -> Message:
                async for event in self.provider.stream(self.messages, [], "m"):
                    if event.type == "done":
                        return event.message
                raise AssertionError("no done event")

            return asyncio.run(run())

        def _ask(self, command: str, call: ToolCall) -> None:
            self.asking = command
            self.pending = call
            self.lines.append(f"⚠ approval required · {command}")

        def _answer(self, choice: str) -> None:
            if self.asking is None:
                return
            command, call = self.asking, self.pending
            self.asking = None
            self.lines = [ln for ln in self.lines if "approval required" not in ln]
            if choice in ("session", "always"):
                self.allowed.add(command)
            if choice == "always":
                cfg = self.cwd / ".k3code" / "config.yaml"
                cfg.parent.mkdir(exist_ok=True)
                cfg.write_text(f"permissions:\n  bash:\n    '{command.split()[0]} *': allow\n")
            self.later(LATENCY, lambda: self._finish(call, choice != "deny"))

        def _finish(self, call: ToolCall, allowed: bool) -> None:
            if allowed:
                touched = re.fullmatch(r"touch (\S+)", call.arguments["command"])
                assert touched, call.arguments
                (self.cwd / touched.group(1)).write_text("")
            self.messages.append(Message(role="tool", content="ok" if allowed else "User denied", tool_call_id=call.id))
            answer = self._call().content or ""
            self.messages.append(Message(role="assistant", content=answer))
            self.lines.append(f"  {answer.strip()}")
            self.lines.append("ready │ m │ default")

    return SimTui


def test_the_permissions_tape_waits_for_each_turns_own_answer(m1, monkeypatch):
    import tui_harness

    monkeypatch.setattr(m1, "Tui", _sim_tui(tui_harness.Tui))
    evidence = m1.perm()
    assert "always ran" in evidence and "turn 4 had no prompt" in evidence


def test_every_turn_of_a_tape_answers_with_a_marker_no_earlier_turn_showed(m1):
    """The fake provider matches a step against the whole conversation, so turn N answers with the markers of turns
    1..N: each turn's own marker is new in that turn and only there."""
    tags = ["tape-1", "tape-2", "tape-3"]
    provider = FakeProvider(steps=[m1.done_step(tag, after_tool=False) for tag in tags])
    messages: list[Message] = []
    answers: list[str] = []
    for tag in tags:
        messages.append(Message(role="user", content=f"go {m1.tape(tag)}"))

        async def run() -> str:
            async for event in provider.stream(messages, [], "m"):
                if event.type == "done":
                    return event.message.content or ""
            return ""

        answers.append(asyncio.run(run()))
        messages.append(Message(role="assistant", content=answers[-1]))
    for n, tag in enumerate(tags):
        assert re.search(m1.done(tag), answers[n]), (tag, answers[n])
        assert not any(re.search(m1.done(tag), earlier) for earlier in answers[:n]), tag
