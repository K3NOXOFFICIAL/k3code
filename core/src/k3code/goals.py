# Port of the design of hermes_cli/goals.py (MIT, Nous Research):
#   Vendored from hermes-agent@4127d78da84b1eee105f298979cc57cc7457f98d:hermes_cli/goals.py (MIT)
# Trimmed for k3code: persistent GoalState per session (stored in session meta), a cheap-model judge
# after each turn (continue/done/blocked), automatic continuation messages, a turn budget as backstop,
# and an optional shell-command gate (--check) that must pass before `done` counts.
# Dropped: contracts, subgoals, wait barriers, delegation/background-process awareness.
"""Persistent session goals — the "Ralph loop".

Judge failures are fail-OPEN (``continue``); the turn budget is the backstop. The continuation is
a normal user message, so the system prompt (and prompt caching) never changes mid-goal.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_MAX_TURNS = 30
DEFAULT_GATE_TIMEOUT_SECONDS = 300
DEFAULT_GATE_MAX_RETRIES = 3
MAX_CONSECUTIVE_PARSE_FAILURES = 3
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 5
_JUDGE_RESPONSE_SNIPPET_CHARS = 4000
_GATE_OUTPUT_TAIL_CHARS = 3000

KICK_PROMPT_TEMPLATE = (
    "[Standing goal]\nGoal: {goal}\n\n"
    "Work toward this goal. Take concrete steps. When the goal is complete, say so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly and stop."
)
CONTINUATION_PROMPT_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Continue working toward this goal. Take the next concrete step. "
    "If you believe the goal is complete, state so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly and stop."
)
CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE = (
    "[Continuing toward your standing goal — the completion check failed]\n"
    "Goal: {goal}\n\n"
    "The check command below must pass before this goal can be declared done, and it just failed "
    "(attempt {attempt}/{max_retries}):\n"
    "  $ {command}\n"
    "Exit code: {exit_code}\n"
    "Output (tail):\n```\n{output}\n```\n\n"
    "Fix the underlying problem so the check passes, then re-run it to confirm. Do not declare the goal "
    "complete while the check fails. If the check itself is wrong or cannot pass, say so clearly and stop."
)

JUDGE_SYSTEM_PROMPT = (
    "You are a strict judge evaluating whether an autonomous agent has achieved a user's stated goal. "
    "You receive the goal text and the agent's most recent response. Decide one of three verdicts.\n\n"
    "DONE — the goal is fully satisfied: the response explicitly confirms completion, or clearly shows the "
    "final deliverable was produced. DONE requires the deliverable to actually exist; if the response only "
    "explains why the goal cannot be reached, the verdict is BLOCKED, not DONE.\n\n"
    "BLOCKED — the goal cannot be satisfied as stated (genuinely unachievable) or progress needs user input.\n\n"
    "CONTINUE — not done, and there is a concrete next step the agent can take right now. "
    "This is the default when in doubt.\n\n"
    "Reply ONLY with a single JSON object on one line:\n"
    '{"verdict": "done", "reason": "<one sentence>"}\n'
    '{"verdict": "blocked", "reason": "<one sentence>"}\n'
    '{"verdict": "continue", "reason": "<one sentence>"}'
)
JUDGE_USER_PROMPT_TEMPLATE = (
    "Goal:\n{goal}\n\nAgent's most recent response:\n{response}\n\nIs the goal satisfied — done, blocked or continue?"
)

#: Judge: (goal, last_response) → (verdict, reason, parse_failed, transport_failed)
Judge = Callable[[str, str], Awaitable[tuple[str, str, bool, bool]]]
#: One-shot model call: (system, user) → text. Raises on transport errors.
Completer = Callable[[str, str], Awaitable[str]]
Reviewer = Callable[[str], Awaitable[tuple[bool, list[str]]]]  # (goal) -> (blocking, issues)

_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class GoalGate:
    """A shell command that must exit 0 before the goal can count as done."""

    command: str
    timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_GATE_MAX_RETRIES
    attempts: int = 0
    last_exit_code: int | None = None
    last_output_tail: str = ""


async def run_gate(gate: GoalGate, *, cwd: str | None = None) -> tuple[bool, int, str]:
    """Run the gate through the shell: ``(passed, exit_code, output_tail)``; a timeout is exit code -1."""
    try:
        proc = await asyncio.create_subprocess_shell(
            gate.command,
            cwd=cwd or None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), max(1, int(gate.timeout_seconds)))
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False, -1, f"[check timed out after {gate.timeout_seconds}s]"
        text = out.decode("utf-8", errors="replace")
        return proc.returncode == 0, proc.returncode or 0, text[-_GATE_OUTPUT_TAIL_CHARS:]
    except Exception as exc:  # noqa: BLE001
        return False, -1, f"[check could not run: {type(exc).__name__}: {exc}]"


@dataclass
class GoalState:
    goal: str
    status: str = "active"  # active | paused | done | cleared
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    created_at: float = 0.0
    last_turn_at: float = 0.0
    last_verdict: str | None = None  # done | blocked | continue | gate_failed
    last_reason: str | None = None
    paused_reason: str | None = None
    consecutive_parse_failures: int = 0
    consecutive_transport_failures: int = 0
    gates: list[GoalGate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalState:
        known = GoalGate.__dataclass_fields__
        gates = [GoalGate(**{k: v for k, v in g.items() if k in known}) for g in data.get("gates") or []]
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "gates"}
        return cls(**fields, gates=gates)

    def snapshot(self) -> dict[str, Any]:
        """Wire shape of the TUI's ``GoalSnapshot`` (``session.control.update``)."""
        return {
            "title": self.goal,
            "status": self.status,
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
            "contract": {},
            "subgoals": [],
            "gates": [
                {
                    "command": g.command,
                    "timeout_seconds": g.timeout_seconds,
                    "max_retries": g.max_retries,
                    "attempts": g.attempts,
                    "last_exit_code": g.last_exit_code,
                }
                for g in self.gates
            ],
            "created_at": self.created_at,
            "updated_at": self.last_turn_at or self.created_at,
            "paused_reason": self.paused_reason,
            "last_verdict": self.last_verdict,
            "last_reason": self.last_reason,
            "wait_barrier": None,
        }


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except Exception:  # noqa: BLE001
            return None
    return data if isinstance(data, dict) else None


def parse_judge_response(raw: str) -> tuple[str, str, bool]:
    """``(verdict, reason, parse_failed)``, fail-open to ``continue``."""
    if not raw:
        return "continue", "judge returned empty response", True
    data = _extract_json_object(raw)
    if data is None:
        return "continue", f"judge reply was not JSON: {_truncate(raw, 200)!r}", True
    reason = str(data.get("reason") or "").strip() or "no reason provided"
    verdict_raw = data.get("verdict")
    if isinstance(verdict_raw, str):
        verdict = verdict_raw.strip().lower()
    else:
        done = data.get("done")
        done = done.strip().lower() in {"true", "yes", "1", "done"} if isinstance(done, str) else bool(done)
        verdict = "done" if done else "continue"
    if verdict not in {"done", "blocked", "continue"}:
        verdict = "continue"
    return verdict, reason, False


def make_judge(complete: Completer) -> Judge:
    """Judge backed by a one-shot model call (the cheap model)."""

    async def judge(goal: str, last_response: str) -> tuple[str, str, bool, bool]:
        if not last_response.strip():
            return "continue", "empty response (nothing to evaluate)", False, False
        user = JUDGE_USER_PROMPT_TEMPLATE.format(
            goal=_truncate(goal, 2000), response=_truncate(last_response, _JUDGE_RESPONSE_SNIPPET_CHARS)
        )
        try:
            raw = await complete(JUDGE_SYSTEM_PROMPT, user)
        except Exception as exc:  # noqa: BLE001 - fail open
            return "continue", f"judge error: {type(exc).__name__}", False, True
        verdict, reason, parse_failed = parse_judge_response(raw)
        return verdict, reason, parse_failed, False

    return judge


@dataclass
class Decision:
    status: str | None
    should_continue: bool
    prompt: str | None
    verdict: str
    reason: str
    message: str


class GoalManager:
    """Goal lifecycle for one session. ``load``/``save`` persist the state dict (session meta)."""

    def __init__(
        self,
        load: Callable[[], dict[str, Any] | None],
        save: Callable[[dict[str, Any] | None], None],
        *,
        default_max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self._load = load
        self._save_raw = save
        self.default_max_turns = default_max_turns

    @property
    def state(self) -> GoalState | None:
        raw = self._load()
        return GoalState.from_dict(raw) if raw else None

    def _save(self, state: GoalState) -> GoalState:
        self._save_raw(state.to_dict())
        return state

    def is_active(self) -> bool:
        s = self.state
        return s is not None and s.status == "active"

    def snapshot(self) -> dict[str, Any] | None:
        s = self.state
        return s.snapshot() if s and s.status != "cleared" else None

    def set(self, goal: str, *, max_turns: int | None = None, check: str | None = None) -> GoalState:
        gates = [GoalGate(command=check)] if check else []
        st = GoalState(
            goal=goal.strip(),
            max_turns=max_turns or self.default_max_turns,
            created_at=time.time(),
            gates=gates,
        )
        return self._save(st)

    def pause(self, reason: str = "user-paused") -> GoalState | None:
        s = self.state
        if s is None or s.status in ("done", "cleared"):
            return s
        s.status, s.paused_reason = "paused", reason
        return self._save(s)

    def resume(self, *, reset_budget: bool = True) -> GoalState | None:
        s = self.state
        if s is None or s.status == "cleared":
            return None
        s.status, s.paused_reason = "active", None
        s.consecutive_parse_failures = s.consecutive_transport_failures = 0
        if reset_budget:
            s.turns_used = 0
        for g in s.gates:
            g.attempts = 0
        return self._save(s)

    def clear(self) -> None:
        self._save_raw(None)

    def status_line(self) -> str:
        s = self.state
        if s is None or s.status == "cleared":
            return "No active goal. Set one with /goal <objective>."
        line = f"Goal ({s.status}, {s.turns_used}/{s.max_turns} turns): {s.goal}"
        if s.gates:
            line += "\n  check: " + "; ".join(f"$ {g.command}" for g in s.gates)
        if s.last_verdict:
            line += f"\n  last verdict: {s.last_verdict} — {s.last_reason}"
        if s.paused_reason:
            line += f"\n  paused: {s.paused_reason}"
        return line

    def kick_prompt(self) -> str | None:
        s = self.state
        return KICK_PROMPT_TEMPLATE.format(goal=s.goal) if s and s.status == "active" else None

    def continuation_prompt(self) -> str | None:
        s = self.state
        return CONTINUATION_PROMPT_TEMPLATE.format(goal=s.goal) if s and s.status == "active" else None

    def _pause_decision(self, s: GoalState, reason: str, verdict: str, why: str, message: str) -> Decision:
        s.status, s.paused_reason = "paused", reason
        self._save(s)
        return Decision("paused", False, None, verdict, why, message)

    async def evaluate_after_turn(
        self, last_response: str, judge: Judge, *, cwd: str | None = None, reviewer: Reviewer | None = None
    ) -> Decision:
        """Judge the finished turn; both user prompts and our continuations spend the turn budget."""
        s = self.state
        if s is None or s.status != "active":
            return Decision(s.status if s else None, False, None, "inactive", "no active goal", "")
        s.turns_used += 1
        s.last_turn_at = time.time()
        verdict, reason, parse_failed, transport_failed = await judge(s.goal, last_response)
        s.last_verdict, s.last_reason = verdict, reason
        s.consecutive_parse_failures = s.consecutive_parse_failures + 1 if parse_failed else 0
        s.consecutive_transport_failures = s.consecutive_transport_failures + 1 if transport_failed else 0

        if verdict == "blocked":
            return self._pause_decision(
                s, f"judged unachievable: {reason}", "blocked", reason,
                f"🚫 Goal judged unachievable — paused: {reason} Re-scope with /goal <objective>, or /goal resume.",
            )

        if verdict == "done":
            for gate in s.gates:  # the check must pass before `done` counts
                passed, code, tail = await run_gate(gate, cwd=cwd)
                gate.last_exit_code, gate.last_output_tail = code, tail
                if passed:
                    gate.attempts = 0
                    continue
                gate.attempts += 1
                s.last_verdict = "gate_failed"
                s.last_reason = f"check failed (exit {code}): $ {gate.command}"
                if gate.attempts > gate.max_retries:
                    return self._pause_decision(
                        s, f"check exhausted {gate.max_retries} retries: $ {gate.command}", "gate_failed",
                        s.last_reason,
                        f"⏸ Goal paused — check still failing after {gate.max_retries} retries: $ {gate.command}",
                    )
                if s.turns_used >= s.max_turns:
                    return self._budget_pause(s, "gate_failed", s.last_reason)
                self._save(s)
                prompt = CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE.format(
                    goal=s.goal, command=gate.command, exit_code=code, attempt=gate.attempts,
                    max_retries=gate.max_retries, output=tail or "(no output)",
                )
                return Decision(
                    "active", True, prompt, "gate_failed", s.last_reason,
                    f"✗ Check failed ({s.turns_used}/{s.max_turns} turns, attempt {gate.attempts}/{gate.max_retries}): "
                    f"$ {gate.command}",
                )
            if reviewer is not None:  # advisor veto: blocking issues keep the goal going
                blocking, issues = await reviewer(s.goal)
                if blocking and issues and s.turns_used < s.max_turns:
                    s.last_verdict = "advisor_blocked"
                    s.last_reason = "advisor: " + "; ".join(issues)[:300]
                    self._save(s)
                    listed = "\n".join(f"- {i}" for i in issues)
                    prompt = (
                        f"[Goal check] A reviewer found blocking issues before the goal could be called done:\n"
                        f"{listed}\nFix them, then confirm the goal is complete. Goal: {s.goal}"
                    )
                    return Decision(
                        "active", True, prompt, "advisor_blocked", s.last_reason,
                        f"⚠ Advisor found blocking issues ({s.turns_used}/{s.max_turns} turns): {issues[0][:120]}",
                    )
            s.status = "done"
            self._save(s)
            return Decision("done", False, None, "done", reason, f"✓ Goal achieved: {reason}")

        if s.consecutive_transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
            n = s.consecutive_transport_failures
            return self._pause_decision(
                s, f"judge model unreachable {n} turns in a row", "continue", reason,
                f"⏸ Goal paused — the judge model returned errors {n} turns in a row (check goal.judge_model).",
            )
        if s.consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
            n = s.consecutive_parse_failures
            return self._pause_decision(
                s, f"judge returned unparseable output {n} turns in a row", "continue", reason,
                f"⏸ Goal paused — the judge isn't returning the required JSON verdict ({n} turns).",
            )
        if s.turns_used >= s.max_turns:
            return self._budget_pause(s, "continue", reason)
        self._save(s)
        return Decision(
            "active", True, self.continuation_prompt(), "continue", reason,
            f"↻ Continuing toward goal ({s.turns_used}/{s.max_turns}): {reason}",
        )

    def _budget_pause(self, s: GoalState, verdict: str, reason: str) -> Decision:
        return self._pause_decision(
            s, f"turn budget exhausted ({s.turns_used}/{s.max_turns})", verdict, reason,
            f"⏸ Goal paused — {s.turns_used}/{s.max_turns} turns used. /goal resume keeps going, /goal clear stops.",
        )
