"""Side calls about a session's own history — titles and compaction — via :class:`ModelCaller`.

Both run on the ``title`` / ``compaction`` task kinds (cheap tier by default), never on the turn router.
"""

from __future__ import annotations

import logging
from typing import Any

from k3code.autonomy.advisor import summary_system, transcript_text
from k3code.context_budget import POINTER_CALL_RE
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

logger = logging.getLogger(__name__)

TITLE_SYSTEM = "Write a 3-6 word title for this coding session. Reply with the title only: no quotes, no period."
SUMMARY_PREFIX = "[summary of earlier conversation]\n"
#: What the summary model reads of the folded part: this many characters (the head and the tail, the middle is cut)
COMPACT_INPUT_CHARS = 60_000
#: Of those, the head: the start of the conversation holds the task statement.
COMPACT_HEAD_CHARS = 8_000
#: The summary the model may write (SUMMARY_SYSTEM asks for 400 words and the paths, commands and errors to keep).
SUMMARY_MAX_TOKENS = 1_000
#: The first task statement is kept verbatim (up to this many characters) so it survives any number of compactions.
TASK_ANCHOR_CHARS = 12_000
#: Of the anchor, the summary model reads this many characters (the anchor itself is kept whole in the result).
ANCHOR_SUMMARY_CHARS = 2_500
#: An anchor over TASK_ANCHOR_CHARS is kept as its first ANCHOR_HEAD_CHARS and its last ANCHOR_TAIL_CHARS characters
#: (room for the note in between); what lies between them is only in the summary.
ANCHOR_TAIL_CHARS = TASK_ANCHOR_CHARS // 3
ANCHOR_HEAD_CHARS = TASK_ANCHOR_CHARS - ANCHOR_TAIL_CHARS - 48
#: What the summary model sees of one folded message: a tool result keeps its start and its end (it used to see the
#: first 3000 characters of everything, which is 11-18% of a long session and none of a test run's last line), a user
#: message up to the anchor's size (a plan or a requirement sent later), an earlier summary whole.
COMPACT_TOOL_CHARS = 400


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
            escalate=False,  # best effort: an outage of the cheap tier is not worth a call on the main one
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
    cut = max(0, len(messages) - max(1, keep))
    while cut > 0 and messages[cut].get("role") != "user":
        cut -= 1
    return cut


def split_between_steps(messages: list[dict[str, Any]], keep: int = 6) -> int:
    """Index of the latest assistant message at or before ``len - keep`` that follows a user message or a tool result.

    For a conversation whose user messages are all early (one big task, a goal turn: hundreds of tool calls after a
    single prompt) split_for_compaction leaves nothing to fold. A cut before such an assistant message separates no
    call from its results. 0 = none."""
    cut = max(0, len(messages) - max(1, keep))
    while cut > 1 and not (
        messages[cut].get("role") == "assistant" and messages[cut - 1].get("role") in ("user", "tool")
    ):
        cut -= 1
    return cut if cut > 1 else 0


def head_and_tail(text: str, limit: int, head: int) -> str:
    """``text`` if it fits ``limit``; otherwise its first ``head`` characters and the last ``limit - head``."""
    if len(text) <= limit:
        return text
    return text[:head] + "\n…[middle of the conversation omitted]…\n" + text[-(limit - head) :]


def _anchor_gist(content: str) -> str:
    """What the summary model reads of the task statement the result keeps verbatim: its start and end. Of one the
    result clips (over TASK_ANCHOR_CHARS) it reads the start of the clipped middle instead (what fits in
    TASK_ANCHOR_CHARS, less when the folded part is long: see head_and_tail): only the summary can carry that."""
    if len(content) <= ANCHOR_SUMMARY_CHARS:
        return content
    if len(content) > TASK_ANCHOR_CHARS:
        middle = content[ANCHOR_HEAD_CHARS : len(content) - ANCHOR_TAIL_CHARS]
        note = "[the start and the end of this message are kept verbatim; the middle, which is not, follows]\n"
        if len(note) + len(middle) > TASK_ANCHOR_CHARS:  # all of it does not fit
            note = note.replace("follows", "follows as far as it fits")
        return note + middle[: TASK_ANCHOR_CHARS - len(note)]
    head = ANCHOR_SUMMARY_CHARS * 3 // 4
    return (
        f"{content[:head]}\n…[the rest of this message is kept verbatim]…\n{content[-(ANCHOR_SUMMARY_CHARS - head) :]}"
    )


def task_anchor(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first user message that is a real task, not an earlier summary."""
    for m in messages:
        content = str(m.get("content") or "")
        if m.get("role") == "user" and content and not content.startswith(SUMMARY_PREFIX):
            return m
    return None


async def compact_messages(
    caller: Any,
    messages: list[dict[str, Any]],
    *,
    keep: int = 6,
    session_id: str = "",
    max_input_chars: int = COMPACT_INPUT_CHARS,
    instructions: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """Summarize the old part of the transcript. Returns (new messages, number of messages folded).

    ``instructions`` is what the user wants the summary to focus on (``/compact <focus>``).

    The summary model reads the head and the tail of the folded part (it used to read only the last characters, so
    the task statement was lost), and the first task statement is kept verbatim ahead of the summary.
    """
    anchor = old = None
    for cut in (split_for_compaction(messages, keep), split_between_steps(messages, keep)):
        old = [m for m in messages[:cut] if m.get("role") != "system"]
        anchor = task_anchor(old)
        # What an earlier compaction left (the task statement and its summary) is not worth a model call alone: it
        # would summarize a summary without freeing a token, on every turn the kept tail alone is over the limit.
        if any(m is not anchor and not str(m.get("content") or "").startswith(SUMMARY_PREFIX) for m in old):
            break
    else:
        return messages, 0
    # The anchor is kept verbatim in the result, so the summary model only needs its gist. At full size it filled the
    # head of the input and pushed the earlier summary, which follows it, into the omitted middle: a second compaction
    # then summarized without what the first had kept.
    shown = [{**m, "content": _anchor_gist(str(m.get("content") or ""))} if m is anchor else m for m in old]
    text = head_and_tail(
        transcript_text(
            shown,
            per_message=3000,
            per_role={"tool": COMPACT_TOOL_CHARS, "user": TASK_ANCHOR_CHARS},
            whole_prefix=SUMMARY_PREFIX,
        ),
        max_input_chars,
        min(COMPACT_HEAD_CHARS, max_input_chars // 4),
    )
    res = await caller.complete(
        TaskKind.COMPACTION,
        [
            Message(role="system", content=summary_system(instructions)),
            Message(role="user", content=text),
        ],
        session_id=session_id,
        max_tokens=SUMMARY_MAX_TOKENS,
        timeout=90,
    )
    if not res.text.strip():  # e.g. a reasoning model that spent its whole budget thinking: keep the history
        raise ValueError("the summary model returned no text")
    summary = {"role": "user", "content": SUMMARY_PREFIX + res.text.strip()}
    system = [m for m in messages[:cut] if m.get("role") == "system"]
    kept = []
    if anchor is not None:
        content = str(anchor.get("content") or "")
        if len(content) > TASK_ANCHOR_CHARS:
            # head and tail inside the same budget: a requirement written after a long paste is at its end. The result
            # stays within the budget, so a later compaction leaves it alone (no growing "omitted" count)
            omitted = len(content) - ANCHOR_HEAD_CHARS - ANCHOR_TAIL_CHARS
            content = f"{content[:ANCHOR_HEAD_CHARS]}\n…[{omitted:,} chars omitted]…\n{content[-ANCHOR_TAIL_CHARS:]}"
        kept = [{**anchor, "content": content}]
    return [*system, *kept, summary, *_without_dangling_pointers(messages[cut:])], cut


DANGLING_POINTER = (
    "[unchanged since an earlier read of this file, which is now only in the summary above: "
    "read it again if you need its text]"
)


def _without_dangling_pointers(tail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``tail`` with every "unchanged since the read" answer whose earlier result is not in it (it was folded into the
    summary, now or by an earlier compaction) reworded: it told the model that content "above still holds" when only a
    summary of it does."""
    here = {m.get("tool_call_id") for m in tail if m.get("role") == "tool"}

    def dangling(m: dict[str, Any]) -> bool:
        hit = POINTER_CALL_RE.match(str(m.get("content") or "")) if m.get("role") == "tool" else None
        return hit is not None and hit.group(1) not in here

    return [{**m, "content": DANGLING_POINTER} if dangling(m) else m for m in tail]
