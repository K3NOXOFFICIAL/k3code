"""Side calls about a session's own history — titles and compaction — via :class:`ModelCaller`.

Both run on the ``title`` / ``compaction`` task kinds (cheap tier by default), never on the turn router.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from k3code.autonomy.advisor import summary_system, transcript_text
from k3code.context_budget import POINTER_CALL_RE, text_tokens
from k3code.errors import ContextOverflow
from k3code.providers.types import Message
from k3code.redact import scrub_text
from k3code.routing.tiers import TaskKind

logger = logging.getLogger(__name__)

TITLE_SYSTEM = "Write a 3-6 word title for this coding session. Reply with the title only: no quotes, no period."
SUMMARY_PREFIX = "[summary of earlier conversation]\n"
#: What the summary model reads of the folded part: this many characters (the head and the tail, the middle is cut)
COMPACT_INPUT_CHARS = 60_000
#: Of those, the head: the start of the conversation holds the task statement.
COMPACT_HEAD_CHARS = 8_000
#: The least the summary model is sent when its window is small.
MIN_SUMMARY_INPUT_CHARS = 2_000
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
#: On a small window the task statement kept verbatim (and the turn's prompt) may take this share of it, but not more
#: than TASK_ANCHOR_CHARS and not less than MIN_ANCHOR_CHARS: at 8k tokens a first message of 12k characters is
#: 3,000 tokens of every request, and the compaction that was to make room could not.
ANCHOR_WINDOW_SHARE = 0.1
MIN_ANCHOR_CHARS = 1_500
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


def _summary_input(transcript: str, max_chars: int, max_tokens: int | None) -> str:
    """The head and the tail of ``transcript`` that the summary model reads: at most ``max_chars`` characters and, when
    given, ``max_tokens`` (estimated: CJK text takes about twice the tokens of the same number of characters)."""
    limit = max_chars
    while True:
        text = head_and_tail(transcript, limit, min(COMPACT_HEAD_CHARS, limit // 4))
        tokens = text_tokens(text) if max_tokens else 0
        if not max_tokens or tokens <= max_tokens or limit <= MIN_SUMMARY_INPUT_CHARS:
            return text
        limit = max(MIN_SUMMARY_INPUT_CHARS, int(limit * max_tokens / tokens * 0.9))


def head_and_tail(text: str, limit: int, head: int) -> str:
    """``text`` if it fits ``limit``; otherwise its first ``head`` characters and the last ``limit - head``."""
    if len(text) <= limit:
        return text
    return text[:head] + "\n…[middle of the conversation omitted]…\n" + text[-(limit - head) :]


def anchor_chars(window: int | None, sample: str = "") -> int:
    """How many characters of the task statement the result keeps: TASK_ANCHOR_CHARS, less on a small ``window``
    (ANCHOR_WINDOW_SHARE of it in tokens). Four characters a token, but for non-ASCII text (CJK takes a token or so a
    character) as ``sample`` measures. Not measured for ASCII (the ellipses of the note in a clipped statement do not
    count): what the result keeps of a statement would measure otherwise than the whole did, and a later compaction
    would clip it again."""
    if not window:
        return TASK_ANCHOR_CHARS
    per_token = 4.0 if sample.replace("…", "").isascii() else len(sample) / (text_tokens(sample) or 1)
    return max(MIN_ANCHOR_CHARS, min(TASK_ANCHOR_CHARS, int(window * ANCHOR_WINDOW_SHARE * per_token)))


def anchor_slack(limit: int) -> int:
    """How far over a scaled ``limit`` a statement may be before it is clipped (or clipped again): the limit of a
    non-ASCII text moves a little between two compactions, and a clipped statement must be left alone by the next
    one. TASK_ANCHOR_CHARS, the fixed limit, has none."""
    return limit // 8 if limit < TASK_ANCHOR_CHARS else 0


def _anchor_gist(content: str, limit: int = TASK_ANCHOR_CHARS) -> str:
    """What the summary model reads of the task statement the result keeps verbatim: its start and end. Of one the
    result clips (over ``limit``) it reads the start of the clipped middle instead (what fits in
    TASK_ANCHOR_CHARS, less when the folded part is long: see head_and_tail): only the summary can carry that."""
    if len(content) <= ANCHOR_SUMMARY_CHARS:
        return content
    if len(content) > limit + anchor_slack(limit):
        tail = limit // 3
        middle = content[limit - tail - 48 : len(content) - tail]
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
    pin: dict[str, Any] | None = None,
    max_input_tokens: int | None = None,
    window: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Summarize the old part of the transcript. Returns (new messages, number of messages folded).

    ``instructions`` is what the user wants the summary to focus on (``/compact <focus>``). ``pin``: a message (the
    prompt of the turn in progress) that is kept verbatim after the summary when it is folded; found by value, since
    a stored turn is written anew at its end. ``max_input_tokens``: the most the summary model is sent (a share of the
    smallest window of the models it can run on); ``max_input_chars`` still applies. ``window``: the smallest window
    of the models the next turn can run on, which the task statement kept verbatim is sized to (see anchor_chars).

    The summary model reads the head and the tail of the folded part (it used to read only the last characters, so
    the task statement was lost), and the first task statement is kept verbatim ahead of the summary.
    """
    best: tuple[int, int, list[dict[str, Any]], dict[str, Any] | None] | None = None
    for cut in (split_for_compaction(messages, keep), split_between_steps(messages, keep)):
        old = [m for m in messages[:cut] if m.get("role") != "system"]
        anchor = task_anchor(old)
        # What an earlier compaction left (the task statement and its summary) is not worth a model call alone: it
        # would summarize a summary without freeing a token, on every turn the kept tail alone is over the limit.
        if not any(m is not anchor and not str(m.get("content") or "").startswith(SUMMARY_PREFIX) for m in old):
            continue
        freed = _tokens([m for m in old if m is not anchor])
        # The cut at a user message is the usual one. A cut between steps (inside the latest turn) is taken instead
        # only when that one frees under half as much: a long single turn (one prompt, hundreds of tool calls) left
        # next to nothing to fold, and a recovery from a context overflow folded 17 of 258 messages.
        if best is None or freed > 2 * best[0]:
            best = (freed, cut, old, anchor)
    if best is None:
        return messages, 0
    _, cut, old, anchor = best
    # The anchor is kept verbatim in the result, so the summary model only needs its gist. At full size it filled the
    # head of the input and pushed the earlier summary, which follows it, into the omitted middle: a second compaction
    # then summarized without what the first had kept.
    limit = anchor_chars(window, str(anchor.get("content") or "") if anchor else "")
    shown = [{**m, "content": _anchor_gist(str(m.get("content") or ""), limit)} if m is anchor else m for m in old]
    transcript = transcript_text(
        shown,
        per_message=3000,
        per_role={"tool": COMPACT_TOOL_CHARS, "user": TASK_ANCHOR_CHARS},
        whole_prefix=SUMMARY_PREFIX,
    )

    async def summarize(text: str) -> Any:
        return await caller.complete(
            TaskKind.COMPACTION,
            [
                Message(role="system", content=summary_system(instructions)),
                Message(role="user", content=text),
            ],
            session_id=session_id,
            max_tokens=SUMMARY_MAX_TOKENS,
            timeout=90,
        )

    text = _summary_input(transcript, max_input_chars, max_input_tokens)
    try:
        res = await summarize(text)
    except ContextOverflow:
        # the summary model's window is smaller than assumed (a model nobody gave a window to): once more, with a
        # quarter of the input, before the compaction is given up
        res = await summarize(_summary_input(transcript, max(MIN_SUMMARY_INPUT_CHARS, len(text) // 4), None))
    if not res.text.strip():  # e.g. a reasoning model that spent its whole budget thinking: keep the history
        raise ValueError("the summary model returned no text")
    body = _without_ledger(res.text.strip())  # a ledger the model restated: ours is built from the calls
    ledger = work_ledger(old)
    summary = {"role": "user", "content": SUMMARY_PREFIX + body + (f"\n\n{ledger}" if ledger else "")}
    system = [m for m in messages[:cut] if m.get("role") == "system"]
    kept = []
    if anchor is not None:
        kept = [{**anchor, "content": _clip_anchor(str(anchor.get("content") or ""), limit)}]
    # the prompt of the turn the fold reaches into stays in view, as the task statement does (it is the task the kept
    # steps belong to; "continue where you stopped" is no task without it)
    pinned = []
    if pin is not None and pin != anchor and any(m == pin for m in messages[:cut]):
        content = str(pin.get("content") or "")
        pinned = [{**pin, "content": _clip_anchor(content, limit)}]
    return [*system, *kept, summary, *pinned, *_without_dangling_pointers(messages[cut:])], cut


#: What the summary ends with besides the model's text: the paths and commands of the tool calls it folded, which a
#: summary of 400 words cannot list (the summary model reads a fixed share of what it folds: a long task changed more
#: files than the summary names, and a second compaction lost the ones the first had named). Built from the calls, so it
#: is the same whatever the model wrote, and carried from one summary to the next.
LEDGER_HEADER = "Work ledger (files and commands of the earlier tool calls):"
#: At most this many of each kind, the newest; one line each.
LEDGER_MAX = {"changed": 28, "ran": 6, "read": 6}
_LEDGER_LINE = re.compile(r"^(changed|read|ran): (.+)$")
_LEDGER_BLOCK = re.compile(
    rf"^{re.escape(LEDGER_HEADER)}\n(?:(?:changed|read|ran): .*\n?|\(older entries not listed\)\n?)*", re.M
)
_WRITE_TOOLS = frozenset({"write", "edit"})


def _without_ledger(text: str) -> str:
    """``text`` without a work ledger block (the summary model was shown the earlier one and may restate it)."""
    return _LEDGER_BLOCK.sub("", text).strip()


def work_ledger(folded: list[dict[str, Any]]) -> str:
    """The ledger of the tool calls in ``folded`` joined to the one an earlier summary among them carries: the files
    changed (newest LEDGER_MAX), the commands run, the files read that were not changed. "" when there is nothing."""
    kinds: dict[str, dict[str, None]] = {"changed": {}, "ran": {}, "read": {}}  # ordered sets, oldest first
    older_dropped = False

    def add(kind: str, item: str) -> None:
        kinds[kind].pop(item, None)  # seen again: it is the newest now
        kinds[kind][item] = None

    for m in folded:
        content = str(m.get("content") or "")
        if content.startswith(SUMMARY_PREFIX) and (block := _LEDGER_BLOCK.search(content)):
            older_dropped = older_dropped or "(older entries not listed)" in block.group(0)
            for line in block.group(0).splitlines():
                if hit := _LEDGER_LINE.match(line):
                    add(hit.group(1), hit.group(2))
        for call in m.get("tool_calls") or []:
            args = call.get("arguments") or {}
            name, path = call.get("name"), args.get("path") or args.get("file_path")
            if name in _WRITE_TOOLS and isinstance(path, str):
                add("changed", path)
            elif name == "read" and isinstance(path, str):
                add("read", path)
            elif name == "bash" and isinstance(args.get("command"), str) and args["command"].strip():
                first, *rest = args["command"].strip().splitlines()
                first = scrub_text(first)  # before the cut: a secret cut in two no longer has its shape
                add("ran", first[:100] + ("…" if rest or len(first) > 100 else ""))
    for path in kinds["changed"]:
        kinds["read"].pop(path, None)  # a file that was changed is listed once
    lines = []
    for kind, items in kinds.items():
        kept = list(items)[-LEDGER_MAX[kind] :]
        older_dropped = older_dropped or len(kept) < len(items)
        lines += [f"{kind}: {item}" for item in kept]
    if not lines:
        return ""
    return "\n".join([LEDGER_HEADER, *lines, *(["(older entries not listed)"] if older_dropped else [])])


def _clip_anchor(content: str, limit: int = TASK_ANCHOR_CHARS) -> str:
    """``content`` as the result keeps it: whole up to ``limit`` (and anchor_slack), else its start and end with a note
    between."""
    if len(content) <= limit + anchor_slack(limit):
        return content
    # head and tail inside the same budget: a requirement written after a long paste is at its end. The result stays
    # within the budget, so a later compaction leaves it alone (no growing "omitted" count)
    tail = limit // 3
    head = limit - tail - 48
    return f"{content[:head]}\n…[{len(content) - head - tail:,} chars omitted]…\n{content[-tail:]}"


def _tokens(messages: list[dict[str, Any]]) -> int:
    """Estimated tokens of stored messages, tool calls included (to compare two cuts of one conversation)."""
    total = 0
    for m in messages:
        total += text_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += text_tokens(str(tc.get("name") or "") + json.dumps(tc.get("arguments") or {}, ensure_ascii=False))
    return total


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
