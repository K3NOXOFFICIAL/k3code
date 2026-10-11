"""How much context a request may use: per-model windows, the compaction threshold and in-turn elision.

Compaction used to start at a fixed 80k estimated tokens whatever the model, counted without the system prompt and
the tool schemas, and only between turns; inside a turn every old tool result was re-sent in full on every call.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
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


def _explicit_window(config: Any, model_id: str) -> int | None:
    """The ``models.<model_id>.context_window`` from config, or None when unset."""
    entry = (getattr(config, "models", None) or {}).get(model_id) or {}
    if isinstance(entry, dict) and entry.get("context_window"):
        return int(entry["context_window"])
    return None


def context_window(config: Any, model_id: str) -> int:
    """Tokens the model ``model_id`` takes: an explicit ``models.<id>.context_window`` entry, else the family
    window, else FALLBACK_WINDOW."""
    return _explicit_window(config, model_id) or family_window(model_id) or FALLBACK_WINDOW


def has_explicit_window(config: Any, model_id: str) -> bool:
    """Whether config sets ``models.<model_id>.context_window``."""
    return _explicit_window(config, model_id) is not None


def family_window(model_id: str) -> int | None:
    """The built-in window of the model's family, or None when no family in FAMILY_WINDOWS matches."""
    name = model_id.lower().rsplit("/", 1)[-1]
    for prefix, window in FAMILY_WINDOWS:
        if name.startswith(prefix):
            return window
    return None


#: What /autocompact accepts as an explicit limit: below the floor every turn would compact (the system prompt and the
#: tool schemas alone are several thousand tokens), above the ceiling the provider rejects the request first.
MIN_COMPACT_TOKENS = 2_000
MIN_COMPACT_RATIO = 0.1
MAX_COMPACT_RATIO = 0.95


@dataclasses.dataclass(frozen=True)
class AutoCompact:
    """When a session is compacted on its own: never (``enabled`` False), at ``tokens`` estimated tokens, at ``ratio``
    of the model's window, or (both None) at COMPACT_AT_RATIO of it."""

    enabled: bool = True
    tokens: int | None = None
    ratio: float | None = None

    @property
    def mode(self) -> str:
        """``off``, ``tokens`` (a fixed limit), ``ratio`` (a share of the window) or ``auto`` (the default share)."""
        if not self.enabled:
            return "off"
        return "tokens" if self.tokens else "ratio" if self.ratio else "auto"


def _positive_int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _share(value: Any) -> float | None:
    try:
        r = float(value)
    except (TypeError, ValueError):
        return None
    return r if 0 < r <= 1 else None


def autocompact_policy(config: Any, override: Mapping[str, Any] | None = None) -> AutoCompact:
    """The automatic-compaction policy: ``override`` (a session's own setting, see /autocompact --session) replaces the
    config's ``context.autocompact`` / ``compact_at_tokens`` / ``compact_at_ratio`` as a whole. A value that is no
    positive number counts as unset: a bad hand-edit must not break a turn."""
    src: Mapping[str, Any] = override or dict(getattr(config, "context", None) or {})
    key = "enabled" if override else "autocompact"
    tokens_key, ratio_key = ("tokens", "ratio") if override else ("compact_at_tokens", "compact_at_ratio")
    return AutoCompact(
        enabled=bool(src.get(key, True)), tokens=_positive_int(src.get(tokens_key)), ratio=_share(src.get(ratio_key))
    )


def compact_threshold(config: Any, model_id: str, policy: AutoCompact | None = None) -> int:
    """Estimated tokens at which a session is compacted: the policy's absolute limit (``context.compact_at_tokens``)
    when set, else its ratio (``context.compact_at_ratio``, default COMPACT_AT_RATIO) of the model's window."""
    policy = policy or autocompact_policy(config)
    if policy.tokens:
        return policy.tokens
    return int(context_window(config, model_id) * (policy.ratio or COMPACT_AT_RATIO))


def text_tokens(text: str) -> int:
    """Estimated tokens of ``text``: ~4 characters per token, plus half a token for every UTF-8 byte past the first of
    a character (CJK text and emoji take about a token per character, of which chars // 4 counted a quarter)."""
    n = len(text)
    if text.isascii():
        return n // 4
    return n // 4 + (len(text.encode("utf-8", "ignore")) - n) // 2


def overhead_tokens(system_prompt: str, specs: Sequence[ToolSpec]) -> int:
    """Estimated tokens of what every request carries besides the conversation: system prompt and tool schemas."""
    schemas = [{"name": s.name, "description": s.description, "parameters": s.parameters} for s in specs]
    return text_tokens(system_prompt) + text_tokens(json.dumps(schemas, ensure_ascii=False))


def message_tokens(messages: Sequence[Message]) -> int:
    """Estimated tokens of messages as sent, tool calls included. The system entry is not counted when it is the
    first message: the request's system prompt is counted by overhead_tokens."""
    if messages and messages[0].role == "system":
        messages = messages[1:]
    tokens = 0
    for m in messages:
        tokens += text_tokens(m.content or "")
        for tc in m.tool_calls:
            tokens += text_tokens(tc.name + (tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False)))
    return tokens


def elide_marker(name: str | None, chars: int) -> str:
    return f"[earlier {name or 'tool'} result elided: {chars} chars — re-run if needed]"


def elision_candidates(
    messages: Sequence[Message], *, elided: set[str], keep: set[str] | frozenset[str] = frozenset()
) -> list[Message]:
    """The tool results a request over the threshold would newly elide (see elide_old_results), in order."""
    tool_idx = [i for i, m in enumerate(messages) if m.role == "tool"]
    old = tool_idx[:-ELIDE_KEEP_CALLS] if len(tool_idx) > ELIDE_KEEP_CALLS else []
    return [
        messages[i]
        for i in old
        if messages[i].tool_call_id not in keep
        and messages[i].tool_call_id not in elided
        and len(messages[i].content or "") > ELIDE_MIN_CHARS
    ]


def elide_old_results(
    messages: Sequence[Message],
    *,
    over: bool,
    elided: set[str],
    keep: set[str] | frozenset[str] = frozenset(),
    notes: dict[str, str] | None = None,
) -> list[Message]:
    """The request with old tool results replaced by a marker; ``messages`` itself is not changed.

    Results older than the last ELIDE_KEEP_CALLS tool results and longer than ELIDE_MIN_CHARS are elided while the
    request is ``over`` the threshold. A result once elided stays elided for the rest of the turn (its id is added to
    ``elided``), so the request prefix does not flip back and forth around the threshold. Ids in ``keep`` are never
    elided (a later "unchanged since" read points at them). ``notes`` (from the decision model, see
    k3code.context_select) holds what is still worth knowing of an elided result; it follows the marker.
    """
    tool_idx = [i for i, m in enumerate(messages) if m.role == "tool"]
    old = set(tool_idx[:-ELIDE_KEEP_CALLS]) if len(tool_idx) > ELIDE_KEEP_CALLS else set()
    out: list[Message] = []
    for i, m in enumerate(messages):
        candidate = i in old and m.tool_call_id not in keep and bool(m.content)
        if candidate and (m.tool_call_id in elided or (over and len(m.content or "") > ELIDE_MIN_CHARS)):
            elided.add(m.tool_call_id or "")
            marker = elide_marker(m.name, len(m.content or ""))
            note = (notes or {}).get(m.tool_call_id or "")
            m = dataclasses.replace(m, content=f"{marker}\nStill relevant: {note}" if note else marker)
        out.append(m)
    return out
