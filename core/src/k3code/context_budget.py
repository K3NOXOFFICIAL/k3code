"""How much context a request may use: per-model windows, the compaction threshold and in-turn elision.

Compaction used to start at a fixed 80k estimated tokens whatever the model, counted without the system prompt and
the tool schemas, and only between turns; inside a turn every old tool result was re-sent in full on every call.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

from k3code.providers.types import Message, ToolSpec

#: Built-in context windows by model family (prefix of the model id, after any "vendor/" part); see context_window.
#: sonnet, opus and haiku are the Claude Code aliases the claude-cli provider sends.
FAMILY_WINDOWS: tuple[tuple[str, int], ...] = (
    ("claude", 200_000),
    ("sonnet", 200_000),
    ("opus", 200_000),
    ("haiku", 200_000),
    ("gpt-4o", 128_000),
    ("gpt-4.1", 128_000),
)
#: Window of an id with no config entry and no known family, mostly a gateway alias (auto/coding-manual) in front of a
#: large model. It was 32k, which compacted those sessions at ~22k tokens. 128k is the floor of current models; a
#: smaller model that overflows is not lost: the gateway compacts on the provider's ContextOverflow and retries once.
#: `k3code doctor` names each id that gets this value.
FALLBACK_WINDOW = 128_000
#: Compaction between turns starts at this share of the active model's window (context.compact_at_ratio).
COMPACT_AT_RATIO = 0.7
#: In a turn, old tool results are elided from the request once it passes this share of the window...
ELIDE_AT_RATIO = 0.5
#: ...except the results of the last ELIDE_KEEP_CALLS tool calls and results of at most ELIDE_MIN_CHARS chars.
ELIDE_KEEP_CALLS = 6
ELIDE_MIN_CHARS = 2000


def context_window(config: Any, model_id: str) -> int:
    """Tokens the model ``model_id`` takes: ``models.<id>.context_window`` from config, else the family default."""
    entry = (getattr(config, "models", None) or {}).get(model_id) or {}
    if isinstance(entry, dict) and entry.get("context_window"):
        return int(entry["context_window"])
    return family_window(model_id) or FALLBACK_WINDOW


def has_explicit_window(config: Any, model_id: str) -> bool:
    """Whether config sets ``models.<model_id>.context_window``."""
    entry = (getattr(config, "models", None) or {}).get(model_id) or {}
    return isinstance(entry, dict) and bool(entry.get("context_window"))


def family_window(model_id: str) -> int | None:
    """The built-in window of the model's family, or None when no family in FAMILY_WINDOWS matches."""
    name = model_id.lower().rsplit("/", 1)[-1]
    for prefix, window in FAMILY_WINDOWS:
        if name.startswith(prefix):
            return window
    return None


def compact_threshold(config: Any, model_id: str) -> int:
    """Estimated tokens at which a session is compacted: ``context.compact_at_tokens`` when set (an absolute
    override), else ``context.compact_at_ratio`` (default COMPACT_AT_RATIO) of the model's window."""
    ctx = dict(getattr(config, "context", None) or {})
    if ctx.get("compact_at_tokens"):
        return int(ctx["compact_at_tokens"])
    return int(context_window(config, model_id) * float(ctx.get("compact_at_ratio", COMPACT_AT_RATIO)))


def overhead_tokens(system_prompt: str, specs: Sequence[ToolSpec]) -> int:
    """Estimated tokens of what every request carries besides the conversation: system prompt and tool schemas."""
    schemas = [{"name": s.name, "description": s.description, "parameters": s.parameters} for s in specs]
    return (len(system_prompt) + len(json.dumps(schemas, ensure_ascii=False))) // 4


def message_tokens(messages: Sequence[Message]) -> int:
    """Estimated tokens of messages as sent (~4 chars per token), tool calls included."""
    chars = 0
    for m in messages:
        chars += len(m.content or "")
        for tc in m.tool_calls:
            chars += len(tc.name) + len(tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False))
    return chars // 4


def elide_marker(name: str | None, chars: int) -> str:
    return f"[earlier {name or 'tool'} result elided: {chars} chars — re-run if needed]"


def elide_old_results(
    messages: Sequence[Message], *, over: bool, elided: set[str], keep: set[str] | frozenset[str] = frozenset()
) -> list[Message]:
    """The request with old tool results replaced by a marker; ``messages`` itself is not changed.

    Results older than the last ELIDE_KEEP_CALLS tool results and longer than ELIDE_MIN_CHARS are elided while the
    request is ``over`` the threshold. A result once elided stays elided for the rest of the turn (its id is added to
    ``elided``), so the request prefix does not flip back and forth around the threshold. Ids in ``keep`` are never
    elided (a later "unchanged since" read points at them).
    """
    tool_idx = [i for i, m in enumerate(messages) if m.role == "tool"]
    old = set(tool_idx[:-ELIDE_KEEP_CALLS]) if len(tool_idx) > ELIDE_KEEP_CALLS else set()
    out: list[Message] = []
    for i, m in enumerate(messages):
        candidate = i in old and m.tool_call_id not in keep and bool(m.content)
        if candidate and (m.tool_call_id in elided or (over and len(m.content or "") > ELIDE_MIN_CHARS)):
            elided.add(m.tool_call_id or "")
            m = dataclasses.replace(m, content=elide_marker(m.name, len(m.content or "")))
        out.append(m)
    return out
