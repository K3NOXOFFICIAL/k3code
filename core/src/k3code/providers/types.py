"""Normalized message / tool-call / stream-event format shared by all providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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

    type: Literal["text_delta", "tool_call", "done", "error"]
    text: str | None = None
    tool_call: ToolCall | None = None
    message: Message | None = None  # set on "done": the full assistant message
    usage: Usage | None = None


ContentPart = str | None

_ROLE_OPENAI = {"system", "user", "assistant", "tool"}


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize messages for chat-completions-compatible providers."""
    out: list[dict[str, Any]] = []
    for m in messages:
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


def messages_to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Split messages into (system, rest) for the Anthropic messages API."""
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
            continue
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
            rest.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id or "",
                            "content": m.content or "",
                        }
                    ],
                }
            )
            continue
        rest.append({"role": "user", "content": m.content or ""})
    return "\n\n".join(system_parts), rest


def _dumps(args: dict[str, Any]) -> str:
    import json

    return json.dumps(args, ensure_ascii=False)
