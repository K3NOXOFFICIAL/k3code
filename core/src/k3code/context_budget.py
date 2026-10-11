"""How much context a request may use: per-model windows, the compaction threshold and in-turn elision.

Compaction used to start at a fixed 80k estimated tokens whatever the model, counted without the system prompt and
the tool schemas, and only between turns; inside a turn every old tool result was re-sent in full on every call.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from typing import Any

from k3code.providers.types import Message, ToolCall, ToolSpec

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
#: A marker is 60-90 chars, so a result past this is a net saving to elide. At 2000 the results of a long turn of small
#: reads and commands were never elided and the turn overflowed after a few hundred calls.
ELIDE_MIN_CHARS = 300
#: The kept results (ELIDE_KEEP_CALLS at most) are also bounded to this share of the window, so a few huge results do
#: not make most of each request "recent" and un-elidable (it then re-triggered, and re-wrote the cache, every step).
ELIDE_KEEP_SHARE = 0.15

#: Every key the code reads under ``context:`` in config.yaml (a misspelled one, such as ``compact_at_token``, is
#: ignored).
CONTEXT_KEYS = frozenset(
    {
        "autocompact",
        "compact_at_ratio",
        "compact_at_tokens",
        "compact_input_chars",
        "decision_model",
        "keep_messages",
        "memory_chars",
        "skill_prompt_limit",
        "tool_output_chars",
    }
)

#: The largest window accepted from config or setup: past this a typo ("1e12") would switch compaction off for good.
MAX_WINDOW = 10_000_000


def parse_window(text: str) -> int | None:
    """Tokens from ``32000``, ``32_000``, ``32,000``, ``32k`` or ``1m``; None for anything else (or a nonsense size)."""
    t = text.strip().lower().replace("_", "").replace(",", "")
    mult = 1000 if t.endswith("k") else 1_000_000 if t.endswith("m") else 1
    try:
        n = int(float(t.removesuffix("k").removesuffix("m")) * mult)
    except (ValueError, OverflowError):  # "abc"; "inf" and "1e400"
        return None
    return n if 1000 <= n <= MAX_WINDOW else None


def _explicit_window(config: Any, model_id: str) -> int | None:
    """The ``models.<model_id>.context_window`` from config, or None when unset or not a usable size (a hand-edited
    ``"200k"`` string works; ``[200000]``, ``-1`` or ``true`` count as unset instead of failing every prompt)."""
    entry = (getattr(config, "models", None) or {}).get(model_id) or {}
    raw = entry.get("context_window") if isinstance(entry, dict) else None
    if raw is None or isinstance(raw, bool | list | dict):
        return None
    return parse_window(str(raw))


def context_window(config: Any, model_id: str) -> int:
    """Tokens the model ``model_id`` takes: an explicit ``models.<id>.context_window`` entry, else the family
    window, else FALLBACK_WINDOW."""
    return _explicit_window(config, model_id) or family_window(model_id) or FALLBACK_WINDOW


def router_window(config: Any, router: Any, fallback_model: str) -> int:
    """The smallest context window among the models ``router`` can send to (a cheap tier or an escalated one has its
    own), so elision starts before the tightest model overflows; ``fallback_model``'s when the router lists none."""
    models = [e.model for e in getattr(router, "chain", None) or [] if getattr(e, "model", None)]
    if not models:
        return context_window(config, fallback_model)
    return min(context_window(config, m) for m in models)


def tool_output_limit(config: Any) -> int | None:
    """``context.tool_output_chars`` from config: how much of one tool result the model is sent; None = the default."""
    return int((getattr(config, "context", None) or {}).get("tool_output_chars", 0) or 0) or None


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


def kept_calls(messages: Sequence[Message], window: int | None = None) -> int:
    """How many of the newest tool results stay whole: ELIDE_KEEP_CALLS, fewer when the newest ones already weigh
    ELIDE_KEEP_SHARE of ``window``. Never fewer than the results of the latest step (the model has not seen them yet)
    and never fewer than one. The count only grows as results are appended, so a result once elided stays elided."""
    if not window:
        return ELIDE_KEEP_CALLS
    budget = window * ELIDE_KEEP_SHARE
    kept = used = 0
    for m in reversed(messages):
        if m.role != "tool":
            continue
        used += text_tokens(m.content or "")
        if used > budget or kept >= ELIDE_KEEP_CALLS:
            break
        kept += 1
    trailing = 0
    for m in reversed(messages):
        if m.role != "tool":
            break
        trailing += 1
    return max(kept, min(trailing, ELIDE_KEEP_CALLS), 1)


def elide_marker(name: str | None, chars: int) -> str:
    return f"[earlier {name or 'tool'} result elided: {chars} chars — re-run if needed]"


def elision_candidates(
    messages: Sequence[Message],
    *,
    elided: set[str],
    keep: set[str] | frozenset[str] = frozenset(),
    window: int | None = None,
) -> list[Message]:
    """The tool results a request over the threshold would newly elide (see elide_old_results), in order."""
    tool_idx = [i for i, m in enumerate(messages) if m.role == "tool"]
    kept = kept_calls(messages, window)
    old = tool_idx[:-kept] if len(tool_idx) > kept else []
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
    window: int | None = None,
) -> list[Message]:
    """The request with old tool results replaced by a marker; ``messages`` itself is not changed.

    Results older than the newest kept_calls() tool results and longer than ELIDE_MIN_CHARS are elided while the
    request is ``over`` the threshold. A result once elided stays elided for the rest of the turn (its id is added to
    ``elided``), so the request prefix does not flip back and forth around the threshold. Ids in ``keep`` are never
    elided (a later "unchanged since" read points at them). ``notes`` (from the decision model, see
    k3code.context_select) holds what is still worth knowing of an elided result; it follows the marker.
    """
    tool_idx = [i for i, m in enumerate(messages) if m.role == "tool"]
    kept = kept_calls(messages, window)
    old = set(tool_idx[:-kept]) if len(tool_idx) > kept else set()
    old_calls = {messages[i].tool_call_id for i in old}
    out: list[Message] = []
    for i, m in enumerate(messages):
        candidate = i in old and m.tool_call_id not in keep and bool(m.content)
        if candidate and (m.tool_call_id in elided or (over and len(m.content or "") > ELIDE_MIN_CHARS)):
            elided.add(m.tool_call_id or "")
            marker = elide_marker(m.name, len(m.content or ""))
            note = (notes or {}).get(m.tool_call_id or "")
            m = dataclasses.replace(m, content=f"{marker}\nStill relevant: {note}" if note else marker)
        elif m.role == "assistant" and m.tool_calls:
            m = _elide_written_text(m, old_calls, over=over, elided=elided)
        out.append(m)
    return out


#: Tools whose arguments are text that is on disk by now: the file a ``write`` made, the strings an ``edit`` swapped.
_FILE_TEXT_ARGS = {"write": ("content",), "edit": ("old_string", "new_string")}


def _call_chars(tc: ToolCall) -> int:
    return sum(len(str(tc.arguments.get(k) or "")) for k in _FILE_TEXT_ARGS.get(tc.name, ()))


def _elide_written_text(m: Message, old_calls: set[str | None], *, over: bool, elided: set[str]) -> Message:
    """``m`` with the file text of its old ``write``/``edit`` calls replaced by a marker. The whole file stayed in the
    request as the call's argument long after the result was elided (ten writes of an 8 KB file: 22 000 tokens,
    resent every step); it is on disk and the model can read it again. Decided together with the results above, so the
    request changes at the same moments, and a call once elided stays elided."""
    calls = list(m.tool_calls)
    changed = False
    for j, tc in enumerate(calls):
        keys = _FILE_TEXT_ARGS.get(tc.name)
        if not keys or tc.id not in old_calls:
            continue
        key = f"args:{tc.id}"  # not the id itself: that one marks the call's *result* as elided
        if key not in elided and not (over and _call_chars(tc) > ELIDE_MIN_CHARS):
            continue
        args = {
            k: (
                f"[{len(str(v))} chars elided: they are in the file now — read it if you need them]" if k in keys else v
            )
            for k, v in tc.arguments.items()
        }
        calls[j] = dataclasses.replace(tc, arguments=args, raw_arguments=None)
        elided.add(key)
        changed = True
    return dataclasses.replace(m, tool_calls=calls) if changed else m
