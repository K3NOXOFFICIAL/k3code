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
    type: Literal["text_delta", "tool_call", "done", "error", "reset"]
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


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize messages for chat-completions-compatible providers."""
    out: list[dict[str, Any]] = []
    for m in normalize_tool_pairs(messages):
        if m.role not in _ROLE_OPENAI:
            raise ValueError(f"unknown role: {m.role}")
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
        out.append(entry)
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
            reminder = {"type": "text", "text": f"<system-reminder>\n{m.content}\n</system-reminder>"}
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
