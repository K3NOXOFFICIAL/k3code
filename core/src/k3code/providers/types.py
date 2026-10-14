"""Normalized message / tool-call / stream-event format shared by all providers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

#: Reserved tool name: a provider could not parse the model's tool-call block. Its arguments carry ``error`` and
#: ``body``; the agent loop answers it with an error result instead of running anything.
INVALID_TOOL_CALL = "invalid_tool_call"


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    # Raw JSON text when the provider gave it unparsed (kept for replay).
    raw_arguments: str | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: List-price cost reported by the provider itself (only the claude-cli provider does); None = unknown.
    cost_usd: float | None = None
    #: Prompt tokens read from / written to the provider's prompt cache (part of prompt_tokens; 0 = none or unknown).
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    #: The model that answered (set by the router; "" = unknown): what the USD budgets price the call by.
    model: str = ""
    #: What a cache-write token costs next to a fresh input token when the provider knows (a 1-hour cache costs 2x, the
    #: 5-minute default 1.25x); None = the price table's default.
    cache_write_multiple: float | None = None


#: An Anthropic prompt-cache breakpoint: the prefix up to and including the block that carries it is cached.
EPHEMERAL = {"type": "ephemeral"}


def cache_marker(ttl: str = "5m") -> dict[str, str]:
    """The ``cache_control`` value for a breakpoint: the default 5-minute entry, or ``ttl`` ("1h") for work that comes
    back after longer (a ``/loop`` or cron job every 5 minutes or more finds a 5-minute entry gone)."""
    return EPHEMERAL if ttl in ("", "5m") else {**EPHEMERAL, "ttl": ttl}


def _can_carry(block: Any) -> bool:
    """Whether a content block can carry a cache breakpoint: not blank text (the API rejects it) and not a tool result
    with nothing in it."""
    body = {"text": "text", "tool_result": "content"}.get(str(block.get("type")))
    value = block.get(body) if body else True
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def with_cache_breakpoint(content: Any, ttl: str = "5m") -> Any:
    """``content`` (a message's string or block list) with a cache breakpoint on its last block that can carry one
    (blank text and an empty tool result are skipped, the breakpoint goes on the block before); unchanged (the same
    object) when it has none."""
    marker = cache_marker(ttl)
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": marker}] if content.strip() else content
    if isinstance(content, list):
        for i in range(len(content) - 1, -1, -1):
            if _can_carry(content[i]):
                return [*content[:i], {**content[i], "cache_control": marker}, *content[i + 1 :]]
    return content


@dataclass
class Message:
    """One conversation message, provider-independent."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # For role == "tool": which call this answers.
    tool_call_id: str | None = None
    name: str | None = None
    usage: Usage | None = None
    #: why the model stopped (provider-neutral; "max_tokens" = cut off at the output limit). Not persisted.
    stop_reason: str | None = None


@dataclass
class ToolSpec:
    """JSON-schema tool description advertised to the model."""

    name: str
    description: str
    parameters: dict[str, Any]
    side_effect: bool = False


@dataclass
class StreamEvent:
    """One streaming event, normalized."""

    #: "reset": the router is retrying or failing over after this attempt already streamed output; consumers discard
    #: the partial text/tool calls they collected (the new attempt streams the whole answer again).
    #: "partial_usage": an attempt died after the provider had counted its prompt (``usage``); the consumer charges it,
    #: the tokens were billed although no answer came of them.
    type: Literal["text_delta", "tool_call", "done", "error", "reset", "partial_usage"]
    text: str | None = None
    tool_call: ToolCall | None = None
    message: Message | None = None  # set on "done": the full assistant message
    usage: Usage | None = None


ContentPart = str | None

_ROLE_OPENAI = {"system", "user", "assistant", "tool"}


def normalize_tool_pairs(messages: list[Message]) -> list[Message]:
    """Make tool calls and tool results pair up, which both wire formats require (else HTTP 400).

    A ``tool`` message is kept only when an earlier assistant message made that call; a call is kept only when a
    later ``tool`` message answers it. Sessions reach providers through storage, bundle import, compaction and
    crash recovery, any of which can leave one half behind. Input messages are not modified.
    """
    answered = {m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id}
    called: set[str] = set()
    out: list[Message] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            kept = [tc for tc in m.tool_calls if tc.id in answered]
            called.update(tc.id for tc in kept)
            if len(kept) != len(m.tool_calls):
                if not kept and not (m.content or "").strip():
                    continue  # nothing left of this message
                m = Message(role=m.role, content=m.content, tool_calls=kept, usage=m.usage)
        elif m.role == "tool" and m.tool_call_id not in called:
            continue
        out.append(m)
    return out


def unique_call_ids(reply: Message, conversation: list[Message]) -> None:
    """Rename, in place, the calls of ``reply`` whose id an earlier call of ``conversation`` already has.

    Some local OpenAI-compatible servers number a reply's calls ``call_0``, ``call_1`` ... afresh every time. Everything
    keyed by the id (the elided results, the notes and pins, the "unchanged since" pointers) then took a new result for
    the old one with the same id. The new id keeps the old one as its start and stays within what Anthropic accepts in
    an id (letters, digits, ``_`` and ``-``); it is made before the reply is stored or echoed back, so the call and its
    result agree everywhere. A provider whose ids are unique (every real one but those servers) is not touched."""
    used = {tc.id for m in conversation if m.role == "assistant" for tc in m.tool_calls}
    for tc in reply.tool_calls:
        if tc.id and tc.id in used:
            n = 1
            while f"{tc.id}_{n}" in used:
                n += 1
            tc.id = f"{tc.id}_{n}"
        used.add(tc.id)


def leading_system_parts(messages: list[Message]) -> list[str]:
    """The text of the system messages the conversation starts with, one entry per message (empty ones left out). A loop
    sends two when the second is what differs between agents that share the first (see ``AgentLoop.system_suffix``)."""
    parts: list[str] = []
    for m in messages:
        if m.role != "system":
            break
        if m.content:
            parts.append(m.content)
    return parts


def _reminder_text(content: str) -> str:
    """A mid-conversation system note as the text a user-side message carries."""
    return f"<system-reminder>\n{content}\n</system-reminder>"


def _append_openai(out: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    """Add ``entry`` to the wire; a plain user message right after another is joined to it: the chat templates of
    Mistral-instruct and Gemma-2 refuse two user turns in a row ('Conversation roles must alternate'), and compaction
    leaves the kept history, the summary and the pinned notes as separate user messages."""
    prev = out[-1] if out else None
    if (
        entry["role"] == "user"
        and prev is not None
        and prev["role"] == "user"
        and isinstance(prev["content"], str)
        and isinstance(entry["content"], str)
    ):
        prev["content"] = "\n\n".join(p for p in (prev["content"], entry["content"]) if p)
        return
    out.append(entry)


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize messages for chat-completions-compatible providers.

    The leading system messages become ONE system message: several in a row are refused by some chat templates (and by
    some servers), and only the native Anthropic API has a use for the split.

    A system message later in the conversation (the loop guard's note) is sent as a ``<system-reminder>`` user message,
    as ``messages_to_anthropic`` does: chat templates that want the system message first (Mistral, Gemma) answer
    HTTP 400 to it, and a relay that hoists it into a Claude system prompt rewrote the start of every request from then
    on. It joins the user message after it when there is one, so user and assistant turns still alternate.
    """
    out: list[dict[str, Any]] = []
    leading = True  # still inside the run of system messages the conversation starts with
    notes: list[str] = []
    for m in normalize_tool_pairs(messages):
        if m.role not in _ROLE_OPENAI:
            raise ValueError(f"unknown role: {m.role}")
        if m.role == "system" and not leading:
            if m.content:
                notes.append(_reminder_text(m.content))
            continue
        leading = leading and m.role == "system"
        if m.role == "system" and out:  # a second leading system message joins the first
            out[-1]["content"] = f"{out[-1]['content']}\n\n{m.content or ''}".rstrip()
            continue
        if m.role == "assistant" and not m.tool_calls and not (m.content or "").strip():
            continue  # said nothing (an empty reply kept in the history): an empty assistant turn is refused by relays
        entry: dict[str, Any] = {"role": m.role, "content": m.content if m.content is not None else ""}
        if m.role == "assistant" and m.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.raw_arguments or _dumps(tc.arguments)},
                }
                for tc in m.tool_calls
            ]
            if m.content is None:
                entry["content"] = None
        if m.role == "tool":
            entry["tool_call_id"] = m.tool_call_id
            if m.name:
                entry["name"] = m.name
        if notes:
            if m.role == "user":
                entry["content"] = "\n\n".join([*notes, *([m.content] if m.content else [])])
            else:
                _append_openai(out, {"role": "user", "content": "\n\n".join(notes)})
            notes = []
        _append_openai(out, entry)
    if notes:  # notes after the last message
        _append_openai(out, {"role": "user", "content": "\n\n".join(notes)})
    return out


def _text_blocks(content: Any) -> list[dict[str, Any]]:
    """A user message's content as a block list (string content becomes one text block, empty text none)."""
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": content}] if content else []


def _attach_reminders(entry: dict[str, Any], reminders: list[dict[str, Any]]) -> None:
    """Add reminder blocks to a user entry: after its tool_result blocks (the API wants those first), else before."""
    blocks = _text_blocks(entry["content"])
    has_results = any(b.get("type") == "tool_result" for b in blocks)
    entry["content"] = blocks + reminders if has_results else reminders + blocks


_ANTHROPIC_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def anthropic_tool_id(tool_id: str) -> str:
    """``tool_id`` as the Anthropic API takes it (letters, digits, ``_`` and ``-``, at most 64 characters).

    A history that crosses providers on failover holds ids the other API made (``functions.read:0``, ``call_9Zx.1``):
    every request after that was refused with HTTP 400. An id that already fits is not touched. Any other becomes its
    characters made safe plus a short hash of the original, so the same id gives the same result in the tool call and
    in its result, in every request, and two different ids never meet."""
    if _ANTHROPIC_ID.fullmatch(tool_id):
        return tool_id
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool_id)[:55]
    return f"{safe}_{hashlib.sha256(tool_id.encode()).hexdigest()[:8]}"


def messages_to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Split messages into (system, rest) for the Anthropic messages API.

    Only the leading system messages become the system prompt. A system message later in the conversation (the loop
    guard's note) is sent as a ``<system-reminder>`` text block inside the adjacent user message: hoisting it into the
    system prompt rewrote the start of every request from then on, so the provider's prompt cache missed for the whole
    rest of the turn. It joins the user message before it (the tool results it follows), else the next one, so it sits
    at the same place in every later request; the API wants user and assistant turns to alternate.
    """
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    leading = True
    pending: list[dict[str, Any]] = []
    results: dict[str, Any] | None = None  # the user message the newest tool results went into
    for m in normalize_tool_pairs(messages):
        if m.role == "system":
            if not m.content:
                continue
            if leading:
                system_parts.append(m.content)
                continue
            reminder = {"type": "text", "text": _reminder_text(m.content)}
            if rest and rest[-1]["role"] == "user":
                _attach_reminders(rest[-1], [reminder])
            else:
                pending.append(reminder)
            continue
        leading = False
        if m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if (m.content or "").strip():  # a whitespace-only text block is refused: HTTP 400 on every later request
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": anthropic_tool_id(tc.id), "name": tc.name, "input": tc.arguments}
                )
            if blocks:
                rest.append({"role": "assistant", "content": blocks})
                results = None
            continue
        if m.role == "tool":
            result = {
                "type": "tool_result",
                "tool_use_id": anthropic_tool_id(m.tool_call_id or ""),
                "content": m.content or "",
            }
            if results is not None:  # the results of one assistant turn belong in ONE user message, in call order
                blocks = results["content"]
                blocks.insert(1 + max(i for i, b in enumerate(blocks) if b.get("type") == "tool_result"), result)
                continue
            entry: dict[str, Any] = {"role": "user", "content": [result]}
        else:
            entry = {"role": "user", "content": m.content if (m.content or "").strip() else ""}
        if pending:
            _attach_reminders(entry, pending)
            pending = []
        if entry["content"] == "":
            continue  # a blank user message: the API refuses it, and the turns around it merge anyway
        rest.append(entry)
        results = entry if m.role == "tool" else None
    if pending:  # notes after an assistant message with no user message after them yet
        rest.append({"role": "user", "content": pending})
    return "\n\n".join(system_parts), rest


def _dumps(args: dict[str, Any]) -> str:
    import json

    return json.dumps(args, ensure_ascii=False)
