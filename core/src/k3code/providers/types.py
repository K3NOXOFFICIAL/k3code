"""Normalized message / tool-call / stream-event format shared by all providers."""

from __future__ import annotations

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


def with_cache_breakpoint(content: Any, ttl: str = "5m") -> Any:
    """``content`` (a message's string or block list) with a cache breakpoint on its last block; unchanged when it has
    no block that can carry one (empty text is rejected by the API; an empty tool result is skipped the same way)."""
    marker = cache_marker(ttl)
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": marker}] if content else content
    if isinstance(content, list) and content:
        last = content[-1]
        body = {"text": "text", "tool_result": "content"}.get(str(last.get("type")))
        if body is None or last.get(body):
            return [*content[:-1], {**last, "cache_control": marker}]
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
                out.append({"role": "user", "content": "\n\n".join(notes)})
            notes = []
        out.append(entry)
    if notes:  # notes after the last message
        out.append({"role": "user", "content": "\n\n".join(notes)})
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
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
            if blocks:
                rest.append({"role": "assistant", "content": blocks})
            continue
        if m.role == "tool":
            entry: dict[str, Any] = {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": m.content or "",
                    }
                ],
            }
        else:
            entry = {"role": "user", "content": m.content or ""}
        if pending:
            _attach_reminders(entry, pending)
            pending = []
        rest.append(entry)
    if pending:  # notes after an assistant message with no user message after them yet
        rest.append({"role": "user", "content": pending})
    return "\n\n".join(system_parts), rest


def _dumps(args: dict[str, Any]) -> str:
    import json

    return json.dumps(args, ensure_ascii=False)
