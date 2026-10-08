"""Replay harness (M1 P6-1): recorded turns replayed through candidate settings on the fake provider.

Every finished turn is recorded with its inputs (the size of each message before each model request, and the size of
the injected memory and skills) and its outcome (status, recorded tokens, whether the verifier passed it).
A candidate setting (a smaller tool-output clip, a memory or skill limit) is replayed: each request is rebuilt with the
candidate applied and sent through :class:`FakeProvider`, whose usage step is the estimated size (characters / 4).
The same records always give the same token totals. A record holds the size of each message, never its text.

The replay cannot re-run the model, so the verifier pass rate is bounded rather than measured: a passing turn whose
inputs the candidate changed counts as a possible failure. That bound is conservative, and it is what the auto-apply
gate reads.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from k3code.providers.fake import FakeProvider
from k3code.providers.types import Message
from k3code.tools import MAX_TOOL_RESULT_CHARS, clip_head_tail

RECORD_PARTS = ("learning", "replay", "turns.jsonl")
#: A turn record larger than this is not kept (records hold sizes only, so this is a guard, not a norm).
MAX_RECORD_CHARS = 200_000
#: The file is cut back to its newest half past this size.
MAX_FILE_BYTES = 40_000_000

#: Decided (D13): an automatic change needs at least this token reduction and at most this pass-rate drop.
AUTO_MIN_TOKEN_REDUCTION_PCT = 15.0
AUTO_MAX_PASS_DROP_POINTS = 1.0

#: Candidate values the optimizer tries (the current value is the default unless the config says otherwise).
TOOL_CLIP_CANDIDATE = 6_000
MEMORY_CANDIDATE = 8_000
SKILLS_CANDIDATE = 20


def est_tokens(chars: int) -> int:
    """The size estimate used everywhere in the replay: about four characters per token."""
    return math.ceil(chars / 4) if chars > 0 else 0


# ── recording ──────────────────────────────────────────────────────────────


class ReplayStore:
    """Append-only JSONL of turn records under ``$K3CODE_HOME/learning/replay/turns.jsonl``."""

    def __init__(self, home: Path | str) -> None:
        self.path = Path(home).joinpath(*RECORD_PARTS)

    def append(self, record: dict[str, Any]) -> bool:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(line) > MAX_RECORD_CHARS:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.path.stat().st_size > MAX_FILE_BYTES:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            self.path.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
        return True

    def load(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn line from a crash is skipped, never fatal
        return out


def build_record(*, turn: str, session: str, kind: str, tier: str, status: str, messages: Sequence[Message],
                 first: int, tokens_in: int, tokens_out: int, memory_chars: int, skill_lines: list[int],
                 ts: float) -> dict[str, Any]:
    """One turn as the replay reads it: the size of every message (never its text), the request boundaries and the
    answer sizes. Request ``k`` of the turn saw ``entries[:boundaries[k]]``; its answer is the assistant message there.

    ``first`` is the index of the turn's own first message (its prompt): the history before it made no request of
    this turn, so its assistant messages are not boundaries (they are still part of every request's input).
    """
    entries = [_entry(m) for m in messages]
    boundaries = [i for i, m in enumerate(messages) if m.role == "assistant" and i >= first]
    completions = [_entry_chars(entries[i]) for i in boundaries]
    return {"turn": turn, "session": session, "ts": ts, "kind": kind, "tier": tier, "status": status,
            "passed": status == "done", "tokens_in": tokens_in, "tokens_out": tokens_out,
            "memory_chars": memory_chars, "skill_lines": list(skill_lines), "entries": entries,
            "boundaries": boundaries, "completions": completions}


def _entry(m: Message) -> dict[str, Any]:
    """Sizes only: a turn record holds no text, so nothing secret can reach the file."""
    out: dict[str, Any] = {"r": m.role, "n": len(m.content or "")}
    if m.tool_calls:
        out["c"] = [len(tc.name) + len(json.dumps(tc.arguments, ensure_ascii=False)) for tc in m.tool_calls]
    return out


def _entry_chars(e: dict[str, Any]) -> int:
    return int(e["n"]) + sum(int(x) for x in e.get("c", []))


@functools.lru_cache(maxsize=4096)
def _clipped_len(n: int, limit: int) -> int:
    """Length of ``clip_head_tail`` applied to a text of ``n`` characters (the clip depends on the length only)."""
    return len(clip_head_tail("x" * n, limit)) if n > limit else n


# ── replay ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A setting to replay. Only the fields that are set change anything."""

    name: str
    clip_chars: int | None = None  # what the model is sent of one tool result
    memory_chars: int | None = None  # the memory section of the system prompt
    skill_limit: int | None = None  # skills named in the system prompt


def _apply(cand: Candidate | None, entries: list[dict[str, Any]], turn: dict[str, Any]
           ) -> tuple[list[dict[str, Any]], bool, int]:
    """The request as the candidate sends it: (entries, changed, characters removed from the system prompt)."""
    if cand is None:
        return entries, False, 0
    changed = False
    out = []
    for e in entries:
        if cand.clip_chars and e["r"] == "tool" and int(e["n"]) > cand.clip_chars:
            e = {**e, "n": _clipped_len(int(e["n"]), cand.clip_chars)}
            changed = True
        out.append(e)
    cut = 0
    if cand.memory_chars is not None and turn["memory_chars"] > cand.memory_chars:
        cut += turn["memory_chars"] - cand.memory_chars
    if cand.skill_limit is not None and len(turn["skill_lines"]) > cand.skill_limit:
        cut += sum(turn["skill_lines"][cand.skill_limit:])
    if cut:
        changed = True
    return out, changed, cut


async def _replay_turn(provider: FakeProvider, turn: dict[str, Any], cand: Candidate | None) -> dict[str, Any]:
    tok_in = tok_out = 0
    changed = False
    entries = turn["entries"]
    for boundary, answer_chars in zip(turn["boundaries"], turn["completions"], strict=True):
        sent, ch, cut = _apply(cand, entries[:boundary], turn)
        changed = changed or ch
        chars = sum(_entry_chars(e) for e in sent) - cut
        # the messages carry roles only: the fake provider needs their shape, the sizes come from the usage step
        messages = [Message(role=e["r"]) for e in sent]
        provider.steps = [{"type": "usage", "prompt_tokens": est_tokens(chars),
                           "completion_tokens": est_tokens(answer_chars)}]
        done = None
        async for event in provider.stream(messages, [], "replay"):
            if event.type == "done":
                done = event
        usage = done.usage if done is not None and done.usage is not None else None
        tok_in += usage.prompt_tokens if usage else 0
        tok_out += usage.completion_tokens if usage else 0
    return {"in": tok_in, "out": tok_out, "changed": changed}


def _fake() -> FakeProvider:
    provider = FakeProvider(name="replay", steps=[])
    provider.record = False  # the replay keeps no prompt log
    return provider


async def evaluate(turns: list[dict[str, Any]], cand: Candidate, *, now_clip: int | None = None) -> dict[str, Any]:
    """Token totals before and after ``cand`` over the recorded turns, and the verifier pass-rate bound.

    "Before" is what the model is sent today: tool results clipped at ``now_clip`` (the configured default when None).
    The memory and skill sizes are already the recorded ones, which were the configured sizes.
    """
    provider = _fake()
    today = Candidate("today", clip_chars=now_clip or MAX_TOOL_RESULT_CHARS)
    before = [await _replay_turn(provider, t, today) for t in turns]
    after = [await _replay_turn(provider, t, cand) for t in turns]
    tok_before = sum(r["in"] + r["out"] for r in before)
    tok_after = sum(r["in"] + r["out"] for r in after)
    n = len(turns)
    passed = sum(1 for t in turns if t["passed"])
    at_risk = sum(1 for t, r in zip(turns, after, strict=True) if t["passed"] and r["changed"])
    return {
        "candidate": cand.name,
        "turns": n,
        "requests": sum(len(t["boundaries"]) for t in turns),
        "tokens_before": tok_before,
        "tokens_after": tok_after,
        "token_reduction_pct": round(100.0 * (tok_before - tok_after) / tok_before, 2) if tok_before else 0.0,
        "changed_turns": sum(1 for r in after if r["changed"]),
        "pass_rate_before": round(passed / n, 4) if n else None,
        "pass_drop_points": round(100.0 * at_risk / n, 2) if n else None,
    }


def evaluate_sync(turns: list[dict[str, Any]], cand: Candidate) -> dict[str, Any]:
    return asyncio.run(evaluate(turns, cand))


def auto_ok(result: dict[str, Any]) -> bool:
    """The auto-apply gate: a real token reduction, a bounded pass-rate drop, and something actually changed."""
    drop = result.get("pass_drop_points")
    return (result["token_reduction_pct"] >= AUTO_MIN_TOKEN_REDUCTION_PCT and drop is not None
            and drop <= AUTO_MAX_PASS_DROP_POINTS and result["changed_turns"] > 0)


def tier_pass_delta(turns: list[dict[str, Any]], kind: str, to_tier: str) -> dict[str, Any]:
    """What moving one task kind to ``to_tier`` does to the verifier pass rate, from the turns observed on each tier.

    Tokens do not depend on the tier, so the token delta is 0; the pass delta is None until both tiers were observed.
    """
    of_kind = [t for t in turns if t["kind"] == kind]
    on_to = [t for t in of_kind if t["tier"] == to_tier]
    elsewhere = [t for t in of_kind if t["tier"] != to_tier]
    delta = None
    if on_to and elsewhere:
        rate_to = sum(t["passed"] for t in on_to) / len(on_to)
        rate_else = sum(t["passed"] for t in elsewhere) / len(elsewhere)
        delta = round(100.0 * (rate_to - rate_else), 2)
    return {"kind": kind, "to_tier": to_tier, "token_delta_pct": 0.0, "pass_delta_points": delta,
            "observed_on_tier": len(on_to), "observed_elsewhere": len(elsewhere)}


async def token_candidates(context: dict[str, Any], turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replayed token-reducing candidates for the current config. Each has a config patch, the replay result and
    ``auto_ok`` (whether the gate allows an automatic change). Candidates that would not reduce anything are dropped."""
    now_clip = int(context.get("tool_output_chars", 10_000))
    now_memory = int(context.get("memory_chars", 20_000))
    now_skills = int(context.get("skill_prompt_limit", 60))
    specs: list[tuple[Candidate, dict[str, Any], str]] = []
    if now_clip > TOOL_CLIP_CANDIDATE:
        specs.append((Candidate("tool_output_chars", clip_chars=TOOL_CLIP_CANDIDATE),
                      {"context": {"tool_output_chars": TOOL_CLIP_CANDIDATE}},
                      f"Clip tool results to {TOOL_CLIP_CANDIDATE} chars for the model (now {now_clip})"))
    if now_memory > MEMORY_CANDIDATE and any(t["memory_chars"] > MEMORY_CANDIDATE for t in turns):
        specs.append((Candidate("memory_chars", memory_chars=MEMORY_CANDIDATE),
                      {"context": {"memory_chars": MEMORY_CANDIDATE}},
                      f"Limit the memory in the prompt to {MEMORY_CANDIDATE} chars (now {now_memory})"))
    if now_skills > SKILLS_CANDIDATE and any(len(t["skill_lines"]) > SKILLS_CANDIDATE for t in turns):
        specs.append((Candidate("skill_prompt_limit", skill_limit=SKILLS_CANDIDATE),
                      {"context": {"skill_prompt_limit": SKILLS_CANDIDATE}},
                      f"List {SKILLS_CANDIDATE} skills in the prompt, not {now_skills}"))
    out = []
    for cand, patch, title in specs:
        result = await evaluate(turns, cand, now_clip=now_clip)
        if result["token_reduction_pct"] <= 0:
            continue
        out.append({"title": title, "patch": patch, "evidence": result, "auto_ok": auto_ok(result)})
    return out
