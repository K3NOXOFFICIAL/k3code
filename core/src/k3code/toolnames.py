"""The name a tool goes by on the wire (shared by the MCP client and the permission rules)."""

from __future__ import annotations

import hashlib

#: Anthropic, OpenAI and Gemini refuse a tool name over 64 characters, and one refused tool fails every request of the
#: session once it is activated (the list is sent each time).
MAX_WIRE_NAME = 64


def wire_name(full: str) -> str:
    """The name sent to the model: ``full`` itself when it fits (existing sessions' tool lists must not move), else
    its first 55 characters + ``_`` + 8 hex digits of its hash (distinct for two long names, stable between runs)."""
    if len(full) <= MAX_WIRE_NAME:
        return full
    return f"{full[: MAX_WIRE_NAME - 9]}_{hashlib.sha256(full.encode()).hexdigest()[:8]}"
