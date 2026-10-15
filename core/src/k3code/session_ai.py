"""Side calls about a session's own history — titles and compaction — via :class:`ModelCaller`.

Both run on the ``title`` / ``compaction`` task kinds (cheap tier by default), never on the turn router. Compaction has
two offline tiers behind the provider model (:func:`compact_with_fallback`): a small local model and an extractive
summary, see :mod:`k3code.compaction`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from k3code.autonomy.advisor import SUMMARY_SYSTEM, transcript_text
from k3code.compaction.extractive import extractive_summary
from k3code.compaction.local import LocalSummarizer
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

logger = logging.getLogger(__name__)

TITLE_SYSTEM = "Write a 3-6 word title for this coding session. Reply with the title only: no quotes, no period."
SUMMARY_PREFIX = "[summary of earlier conversation]\n"
#: What the summary model reads of the folded part: this many characters (the head and the tail, the middle is cut)
COMPACT_INPUT_CHARS = 60_000
#: Of those, the head: the start of the conversation holds the task statement.
COMPACT_HEAD_CHARS = 8_000
#: The first task statement is kept verbatim (up to this many characters) so it survives any number of compactions.
TASK_ANCHOR_CHARS = 12_000
#: The extractive summary (no model) is at most this long: about the size of four provider summaries.
EXTRACTIVE_CHARS = 12_000


def clean_title(raw: str) -> str:
    line = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    return line.strip(" \t\"'`*#.")[:80]


async def make_title(caller: Any, first_message: str, *, session_id: str = "") -> str:
    """A short title for a new session; "" when the model fails (titles are best-effort)."""
    try:
        res = await caller.complete(
            TaskKind.TITLE,
            [Message(role="system", content=TITLE_SYSTEM), Message(role="user", content=first_message[:2000])],
            session_id=session_id,
            max_tokens=24,
            timeout=20,
        )
    except Exception:  # noqa: BLE001
        logger.debug("title generation failed", exc_info=True)
        return ""
    return clean_title(res.text)


def split_for_compaction(messages: list[dict[str, Any]], keep: int = 6) -> int:
    """Index where the kept tail starts: the latest user turn boundary at or before ``len - keep``.

    Cutting only at a user message keeps assistant tool calls together with their tool results.
    """
    cut = max(0, len(messages) - keep)
    while cut > 0 and messages[cut].get("role") != "user":
        cut -= 1
    return cut


def head_and_tail(text: str, limit: int, head: int) -> str:
    """``text`` if it fits ``limit``; otherwise its first ``head`` characters and the last ``limit - head``."""
    if len(text) <= limit:
        return text
    return text[:head] + "\n…[middle of the conversation omitted]…\n" + text[-(limit - head) :]


def task_anchor(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first user message that is a real task, not an earlier summary."""
    for m in messages:
        content = str(m.get("content") or "")
        if m.get("role") == "user" and content and not content.startswith(SUMMARY_PREFIX):
            return m
    return None


def fold_span(messages: list[dict[str, Any]], keep: int, max_input_chars: int) -> tuple[int, list[dict[str, Any]], str]:
    """(cut, the folded messages, what a summary model reads of them). ``old`` is empty when nothing folds."""
    cut = split_for_compaction(messages, keep)
    old = [m for m in messages[:cut] if m.get("role") != "system"]
    if not old:
        return cut, [], ""
    text = head_and_tail(
        transcript_text(old, per_message=3000), max_input_chars, min(COMPACT_HEAD_CHARS, max_input_chars // 4)
    )
    return cut, old, text


def assemble(
    messages: list[dict[str, Any]], cut: int, old: list[dict[str, Any]], summary_text: str
) -> list[dict[str, Any]]:
    """The compacted transcript: the system entries, the task statement verbatim, the summary, then the kept tail."""
    summary = {"role": "user", "content": SUMMARY_PREFIX + summary_text.strip()}
    system = [m for m in messages[:cut] if m.get("role") == "system"]
    anchor = task_anchor(old)
    kept = []
    if anchor is not None:
        content = str(anchor.get("content") or "")
        if len(content) > TASK_ANCHOR_CHARS:
            content = content[:TASK_ANCHOR_CHARS] + "\n…[task statement truncated]"
        kept = [{**anchor, "content": content}]
    return [*system, *kept, summary, *messages[cut:]]


async def provider_summary(caller: Any, text: str, *, session_id: str = "") -> tuple[str, str]:
    """(summary, model label) from the ``compaction`` tier."""
    res = await caller.complete(
        TaskKind.COMPACTION,
        [
            Message(role="system", content=SUMMARY_SYSTEM),
            Message(role="user", content=text),
        ],
        session_id=session_id,
        max_tokens=700,
        timeout=90,
    )
    return res.text, str(getattr(res, "model", "") or "")


async def compact_messages(
    caller: Any,
    messages: list[dict[str, Any]],
    *,
    keep: int = 6,
    session_id: str = "",
    max_input_chars: int = COMPACT_INPUT_CHARS,
) -> tuple[list[dict[str, Any]], int]:
    """Summarize the old part of the transcript with the provider model. Returns (new messages, messages folded).

    The summary model reads the head and the tail of the folded part (it used to read only the last characters, so
    the task statement was lost), and the first task statement is kept verbatim ahead of the summary. Raises when
    the provider fails; :func:`compact_with_fallback` adds the offline tiers.
    """
    cut, old, text = fold_span(messages, keep, max_input_chars)
    if not old:
        return messages, 0
    summary, _ = await provider_summary(caller, text, session_id=session_id)
    return assemble(messages, cut, old, summary), cut


#: Tier names of :class:`CompactionResult`.
TIER_PROVIDER, TIER_LOCAL, TIER_EXTRACTIVE = "provider", "local", "extractive"
EXTRACTIVE_LABEL = "extractive (no model reachable)"


@dataclass
class CompactionResult:
    messages: list[dict[str, Any]]
    folded: int
    #: "provider" | "local" | "extractive"; "" when nothing folded
    tier: str = ""
    #: what the event says: ``claude-sonnet``, ``local qwen2.5-1.5b``, ``extractive (no model reachable)``
    label: str = ""

    @property
    def note(self) -> str:
        """``compacted with claude-sonnet`` / ``compacted with local qwen2.5-1.5b`` / ``compacted extractive (…)``."""
        return f"compacted {self.label}" if self.tier == TIER_EXTRACTIVE else f"compacted with {self.label}"


async def compact_with_fallback(
    caller: Any,
    messages: list[dict[str, Any]],
    *,
    keep: int = 6,
    session_id: str = "",
    max_input_chars: int = COMPACT_INPUT_CHARS,
    local: LocalSummarizer | None = None,
    prefer_local: bool = False,
    offline: bool = False,
    extractive_chars: int = EXTRACTIVE_CHARS,
) -> CompactionResult:
    """Compaction that works offline: the provider model, then the local model, then an extractive summary.

    The provider tier is skipped while the network is ``offline`` (the net guard says so) and falls through on any
    failure (every provider down, rate limits, a timeout). With ``prefer_local`` the local model goes first, so an
    offline session never waits for a provider. The extractive tier needs no model and cannot fail, so a session at
    the context limit always gets compacted; the kept tail (the latest ``keep`` messages) is the same in every tier.
    """
    cut, old, text = fold_span(messages, keep, max_input_chars)
    if not old:
        return CompactionResult(messages, 0)

    async def via_provider() -> tuple[str, str] | None:
        if offline:
            logger.info("compaction: offline, the provider tier is skipped")
            return None
        try:
            summary, model = await provider_summary(caller, text, session_id=session_id)
        except Exception:  # noqa: BLE001 - every provider unreachable, parked or rate-limited: try the next tier
            logger.warning("compaction with the provider model failed; falling back", exc_info=True)
            return None
        return summary, model or "the provider model"

    async def via_local() -> tuple[str, str] | None:
        if local is None:
            return None
        try:
            return await local.summarize(text), local.label
        except Exception:  # noqa: BLE001 - a broken model file, out of memory: the extractive tier still works
            logger.warning("compaction with the local model failed; falling back", exc_info=True)
            return None

    order = [(TIER_LOCAL, via_local), (TIER_PROVIDER, via_provider)]
    if not prefer_local:
        order.reverse()
    for tier, attempt in order:
        got = await attempt()
        if got is not None and got[0].strip():
            return CompactionResult(assemble(messages, cut, old, got[0]), cut, tier, got[1])
    summary = extractive_summary(old, budget_chars=extractive_chars)
    return CompactionResult(assemble(messages, cut, old, summary), cut, TIER_EXTRACTIVE, EXTRACTIVE_LABEL)
