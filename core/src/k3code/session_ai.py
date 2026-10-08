"""Side calls about a session's own history — titles and compaction — via :class:`ModelCaller`.

Both run on the ``title`` / ``compaction`` task kinds (cheap tier by default), never on the turn router.
"""

from __future__ import annotations

import logging
from typing import Any

from k3code.autonomy.advisor import SUMMARY_SYSTEM, transcript_text
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


def clean_title(raw: str) -> str:
    line = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    return line.strip(" \t\"'`*#.")[:80]


async def make_title(caller: Any, first_message: str, *, session_id: str = "") -> str:
    """A short title for a new session; "" when the model fails (titles are best-effort)."""
    try:
        res = await caller.complete(
            TaskKind.TITLE,
            [Message(role="system", content=TITLE_SYSTEM), Message(role="user", content=first_message[:2000])],
            session_id=session_id, max_tokens=24, timeout=20,
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
    return text[:head] + "\n…[middle of the conversation omitted]…\n" + text[-(limit - head):]


def task_anchor(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first user message that is a real task, not an earlier summary."""
    for m in messages:
        content = str(m.get("content") or "")
        if m.get("role") == "user" and content and not content.startswith(SUMMARY_PREFIX):
            return m
    return None


async def compact_messages(
    caller: Any, messages: list[dict[str, Any]], *, keep: int = 6, session_id: str = "",
    max_input_chars: int = COMPACT_INPUT_CHARS,
) -> tuple[list[dict[str, Any]], int]:
    """Summarize the old part of the transcript. Returns (new messages, number of messages folded).

    The summary model reads the head and the tail of the folded part (it used to read only the last characters, so
    the task statement was lost), and the first task statement is kept verbatim ahead of the summary.
    """
    cut = split_for_compaction(messages, keep)
    old = [m for m in messages[:cut] if m.get("role") != "system"]
    if not old:
        return messages, 0
    text = head_and_tail(transcript_text(old, per_message=3000), max_input_chars,
                         min(COMPACT_HEAD_CHARS, max_input_chars // 4))
    res = await caller.complete(
        TaskKind.COMPACTION,
        [
            Message(role="system", content=SUMMARY_SYSTEM),
            Message(role="user", content=text),
        ],
        session_id=session_id, max_tokens=700, timeout=90,
    )
    summary = {"role": "user", "content": SUMMARY_PREFIX + res.text.strip()}
    system = [m for m in messages[:cut] if m.get("role") == "system"]
    anchor = task_anchor(old)
    kept = []
    if anchor is not None:
        content = str(anchor.get("content") or "")
        if len(content) > TASK_ANCHOR_CHARS:
            content = content[:TASK_ANCHOR_CHARS] + "\n…[task statement truncated]"
        kept = [{**anchor, "content": content}]
    return [*system, *kept, summary, *messages[cut:]], cut
