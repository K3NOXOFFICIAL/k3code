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


async def compact_messages(
    caller: Any, messages: list[dict[str, Any]], *, keep: int = 6, session_id: str = ""
) -> tuple[list[dict[str, Any]], int]:
    """Summarize the old part of the transcript. Returns (new messages, number of messages folded)."""
    cut = split_for_compaction(messages, keep)
    old = [m for m in messages[:cut] if m.get("role") != "system"]
    if not old:
        return messages, 0
    res = await caller.complete(
        TaskKind.COMPACTION,
        [
            Message(role="system", content=SUMMARY_SYSTEM),
            Message(role="user", content=transcript_text(old, per_message=3000)[-60000:]),
        ],
        session_id=session_id, max_tokens=700, timeout=90,
    )
    summary = {"role": "user", "content": SUMMARY_PREFIX + res.text.strip()}
    system = [m for m in messages[:cut] if m.get("role") == "system"]
    return [*system, summary, *messages[cut:]], cut
