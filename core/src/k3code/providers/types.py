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
#: The same with the 1-hour TTL (no beta header needed): written at 2x the input price instead of 1.25x, read alike.
EPHEMERAL_1H = {"type": "ephemeral", "ttl": "1h"}
#: ``Message.meta`` key on the leading system message: ``"5m"`` keeps the system breakpoint at 5m even where the
#: provider uses 1h for the stable prefix (a sub-agent's prompt is unique to it, so a 1h write there is never read back)
SYSTEM_CACHE_KEY = "system_cache_ttl"


def with_cache_breakpoint(content: Any) -> Any:
    """``content`` (a message's string or block list) with a cache breakpoint on its last block; unchanged when it has
    no block that can carry one (empty text is rejected by the API; an empty tool result is skipped the same way)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": EPHEMERAL}] if content else content
    if isinstance(content, list) and content:
        last = content[-1]
        body = {"text": "text", "tool_result": "content"}.get(str(last.get("type")))
        if body is None or last.get(body):
            return [*content[:-1], {**last, "cache_control": EPHEMERAL}]
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
    #: the harness's own notes on the message, stored with it and never sent to a provider: ``{"continuation": True}``
    #: marks a user message the harness wrote to continue a goal (see GatewayServer._run_turn_locked, session.undo)
    meta: dict[str, Any] | None = None


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
    #: set on "done" by the router: ``{"provider", "model", "hops"}`` of the entry that answered; ``hops`` lists the
    #: entries passed over on the way, in order, as ``{"provider", "model", "reason"}`` (a FailoverReason value)
    route: dict[str, Any] | None = None


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


def _reminder_text(content: str) -> str:
    """A mid-conversation system note as the text a user-side message carries."""
    return f"<system-reminder>\n{content}\n</system-reminder>"


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize messages for chat-completions-compatible providers.

    Only the leading system messages stay ``system`` messages. A system message later in the conversation (the loop
    guard's note) is sent as a ``<system-reminder>`` user message, as ``messages_to_anthropic`` does: chat templates
    that want the system message first (Mistral, Gemma) answer HTTP 400 to it, and a relay that hoists it into a
    Claude system prompt rewrote the start of every request from then on. It joins the user message after it when
    there is one, so user and assistant turns still alternate.
    """
    out: list[dict[str, Any]] = []
    leading = True
    notes: list[str] = []
    for m in normalize_tool_pairs(messages):
        if m.role not in _ROLE_OPENAI:
            raise ValueError(f"unknown role: {m.role}")
        if m.role == "system" and not leading:
            if m.content:
                notes.append(_reminder_text(m.content))
            continue
        leading = leading and m.role == "system"
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
