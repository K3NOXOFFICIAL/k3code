"""Wake words: a user prompt that names an ultra mode (``ultracode``, ``ultraplan``, ``ultraresearch``) runs that mode.

``detect(text)`` is the single definition of what counts as a mention. The TUI mirrors it in
``tui/shared/wake-words.ts`` (composer highlight); both are tested against ``tests/data/wake_word_cases.json``, so a
change here must change the table and the TypeScript copy together.

A mention triggers only when it is unambiguous:

* the prompt is not a slash command (``/ultracode fix x`` already runs the command itself);
* the word stands alone: not part of a longer word, a path (``src/ultracode.py``), a flag (``--ultracode``), an
  ``@mention`` or a ``#tag``;
* it is not quoted or in code: a fenced block, an inline code span, or wrapped in quotes or backticks;
* the prompt does not name two different modes ("what is the difference between ultracode and ultraplan?").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: wake word (lower case) -> the slash command it runs
WAKE_WORDS: dict[str, str] = {
    "ultracode": "ultracode",
    "ultraplan": "ultraplan",
    "ultraresearch": "ultraresearch",
}

# ASCII classes and re.ASCII on purpose: the TypeScript copy has no Unicode-aware \w, and re.I would otherwise fold
# characters such as the long s onto "s".
_WORD = re.compile(
    r"(?<![A-Za-z0-9_/.@#$:\\-])(" + "|".join(WAKE_WORDS) + r")(?![A-Za-z0-9_/\\-])(?!\.[A-Za-z0-9_])",
    re.IGNORECASE | re.ASCII,
)
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_QUOTES = "`\"'“”‘’"
_LEAD_PUNCT = re.compile(r"^[ \t]*[:,;–—-](?=\s|$)")
_TRAIL_PUNCT = ",;:-–—"
_GLUE_AFTER = ",.;:!?)"
_ONLY_PUNCT = re.compile(r"[\s!-/:-@\[-`{-~]*")  # nothing to do but punctuation: the word stood alone


#: ``wake_words`` config keys: ``enabled`` switches all of them, a word's own key switches that one (all default on)
CONFIG_KEYS = ("enabled", *WAKE_WORDS)


@dataclass(frozen=True)
class WakeMatch:
    mode: str  #: lower-case wake word, which is also the name of the command it runs
    start: int  #: offset of the word in the prompt
    end: int
    task: str  #: the prompt without the word (may be empty: the word alone)


def _masked(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _FENCE.finditer(text)]
    spans += [m.span() for m in _INLINE_CODE.finditer(text) if not any(a <= m.start() < b for a, b in spans)]
    return spans


def _task_without(text: str, start: int, end: int) -> str:
    before, after = text[:start], text[end:]
    if not before.strip():
        task = _LEAD_PUNCT.sub("", after)  # "ultracode: fix it" -> "fix it"
    elif not after.strip():
        task = before.rstrip().rstrip(_TRAIL_PUNCT)
    else:
        joined = before.rstrip(" \t")
        sep = "" if joined.endswith("\n") or after[:1] in tuple(_GLUE_AFTER) else " "
        task = joined + sep + after.lstrip(" \t")
    task = task.strip()
    return "" if _ONLY_PUNCT.fullmatch(task) else task


def detect(text: str) -> WakeMatch | None:
    """The wake word in ``text`` and the task that is left, or None (see the module docstring for the rules)."""
    if text.lstrip().startswith("/"):
        return None
    masked = _masked(text)
    found: list[re.Match[str]] = []
    for m in _WORD.finditer(text):
        if any(a <= m.start() < b for a, b in masked):
            continue
        if (m.start() > 0 and text[m.start() - 1] in _QUOTES) or (m.end() < len(text) and text[m.end()] in _QUOTES):
            continue
        found.append(m)
    if not found or len({m.group(1).lower() for m in found}) > 1:
        return None
    first = found[0]
    task = _task_without(text, first.start(), first.end())
    return WakeMatch(first.group(1).lower(), first.start(), first.end(), task)


def wake_cfg(config: Any) -> dict[str, bool]:
    """``config.wake_words`` over the defaults (everything on); a value that is not a boolean keeps its default."""
    raw = getattr(config, "wake_words", None) or {}
    out = dict.fromkeys(CONFIG_KEYS, True)
    for key in out:
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    return out


def detect_enabled(config: Any, text: str) -> WakeMatch | None:
    """:func:`detect`, unless the config switched wake words (or the mode it found) off."""
    cfg = wake_cfg(config)
    if not cfg["enabled"]:
        return None
    hit = detect(text)
    return hit if hit is not None and cfg[hit.mode] else None
