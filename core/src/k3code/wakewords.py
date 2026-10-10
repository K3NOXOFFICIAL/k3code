"""Wake words: a user prompt that names an ultra mode (``ultracode``, ``ultraplan``, ``ultraresearch``) runs that mode.

``detect(text)`` is the single definition of what counts as a mention. The TUI mirrors it in
``tui/shared/wake-words.ts`` (composer highlight); both are tested against ``tests/data/wake_word_cases.json``, so a
change here must change the table and the TypeScript copy together.

A mention triggers only when it is unambiguous:

* the prompt is not a slash command (``/ultracode fix x`` already runs the command itself);
* the word stands alone: not part of a longer word (in any script), a path (``src/ultracode.py``), a flag
  (``--ultracode``), an ``@mention`` or a ``#tag``;
* it is not quoted or in code: a fenced (backtick or ``~~~``) or indented block, an inline code span, or wrapped in
  quotes or backticks, and it is not in pasted text (``skip``);
* the prompt does not name two different modes ("what is the difference between ultracode and ultraplan?").
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: wake word (lower case) -> the slash command it runs
WAKE_WORDS: dict[str, str] = {
    "ultracode": "ultracode",
    "ultraplan": "ultraplan",
    "ultraresearch": "ultraresearch",
}

# ASCII classes and re.ASCII on purpose: the TypeScript copy has no Unicode-aware \w, and re.I would otherwise fold
# characters such as the long s onto "s". A non-ASCII letter, digit or combining mark next to the word is checked
# separately (_glued), with Unicode categories that both copies read the same way.
_WORD = re.compile(
    r"(?<![A-Za-z0-9_/.@#$:\\-])(" + "|".join(WAKE_WORDS) + r")(?![A-Za-z0-9_/\\-])(?!\.[A-Za-z0-9_])",
    re.IGNORECASE | re.ASCII,
)
#: Whitespace, spelled out so both copies agree: Python's str.isspace() set plus U+FEFF (JavaScript's \s and trim()
#: include U+FEFF but not U+001C-U+001F and U+0085; Python's the other way round). _WS is the same set as a string.
_WS_CLASS = r"\t\n\x0b\x0c\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_WS = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    + "".join(chr(c) for c in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_END = r"(?![\s\S])"  # end of text, written the same in both copies (Python's \Z and $ differ from JavaScript's)
#: ``` fences anywhere, and ~~~ fences that open a line (an unclosed fence runs to the end)
_FENCE = re.compile(r"```[\s\S]*?(?:```|" + _END + r")|(?<![^\n])[ ]{0,3}~~~[\s\S]*?(?:\n[ ]{0,3}~~~|" + _END + ")")
#: indented code: lines indented by four spaces or a tab, after a blank line or at the start (as in CommonMark, an
#: indented line does not interrupt a paragraph); group 1 is the block
_INDENT = r"(?:[ ]{0,3}\t|[ ]{4})"
_INDENTED = re.compile(
    r"(?:^|\n[ \t]*\n)((?:" + _INDENT + r"[^\n]*(?:\n|" + _END + r")|[ \t]*\n(?=" + _INDENT + r"))+)"
)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_QUOTES = "`\"'“”‘’„‚«»‹›「」『』"
#: brackets and emphasis wrapped straight around the word go with it: "(ultracode) fix it" -> "fix it"
_WRAPPERS = {"(": ")", "[": "]", "{": "}", "<": ">", "*": "*", "~": "~"}
_LEAD_PUNCT = re.compile(r"^[ \t]*[:,;–—-](?=[" + _WS_CLASS + "]|" + _END + ")")
_TRAIL_PUNCT = ",;:-–—"
_GLUE_AFTER = ",.;:!?)"
_ONLY_PUNCT = re.compile(r"[" + _WS_CLASS + r"!-/:-@\[-`{-~]*")  # nothing to do but punctuation


#: ``wake_words`` config keys: ``enabled`` switches all of them, a word's own key switches that one (all default on)
CONFIG_KEYS = ("enabled", *WAKE_WORDS)


@dataclass(frozen=True)
class WakeMatch:
    mode: str  #: lower-case wake word, which is also the name of the command it runs
    start: int  #: offset of the word in the prompt
    end: int
    task: str  #: the prompt without the word (may be empty: the word alone)


class _Spans:
    """Sorted, non-overlapping ``(start, end)`` spans and a logarithmic "is this offset in one of them" test, so a long
    prompt full of code is not scanned once per word."""

    def __init__(self, spans: list[tuple[int, int]]) -> None:
        self.spans = spans
        self.starts = [a for a, _ in spans]

    def __contains__(self, pos: int) -> bool:
        i = bisect_right(self.starts, pos) - 1
        return i >= 0 and pos < self.spans[i][1]


def _merge(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        elif a < b:
            out.append((a, b))
    return out


def _masked(text: str, skip: list[tuple[int, int]]) -> _Spans:
    """Everything a wake word does not count in: ``skip`` (pasted text), fenced and indented code blocks, and the inline
    code spans that do not start inside one of those. Pasted text is blanked out before the code is looked for, so a
    backtick in a paste never opens a fence over what was typed after it."""
    scan = text
    if skip:
        parts, pos = [], 0
        for a, b in skip:
            parts += [text[pos:a], "\0" * (b - a)]
            pos = b
        scan = "".join([*parts, text[pos:]])
    blocks = _merge([*skip, *(m.span() for m in _FENCE.finditer(scan)), *(m.span(1) for m in _INDENTED.finditer(scan))])
    in_block = _Spans(blocks)
    inline = [m.span() for m in _INLINE_CODE.finditer(scan) if m.start() not in in_block]
    return _Spans(_merge([*blocks, *inline]))


def _glued(ch: str) -> bool:
    """A letter, digit or combining mark of any script: the word next to it is part of a longer word."""
    return unicodedata.category(ch)[0] in "LNM"


def _task_without(text: str, start: int, end: int) -> str:
    while start > 0 and end < len(text) and _WRAPPERS.get(text[start - 1]) == text[end]:
        start, end = start - 1, end + 1
    before, after = text[:start], text[end:]
    if not before.strip(_WS):
        task = _LEAD_PUNCT.sub("", after)  # "ultracode: fix it" -> "fix it"
    elif _ONLY_PUNCT.fullmatch(after):  # "fix it, ultracode." -> "fix it."
        task = before.rstrip(_WS).rstrip(_TRAIL_PUNCT).rstrip(_WS) + after.strip(_WS)
    else:
        joined = before.rstrip(" \t")
        rest = after.lstrip(" \t")
        if joined.endswith("\n") or joined[-1:] in _TRAIL_PUNCT:  # "Hey, ultracode: fix it" -> "Hey, fix it"
            rest = _LEAD_PUNCT.sub("", rest).lstrip(" \t")
        glue = joined.endswith("\n") or joined[-1:] in tuple("([{<") or rest[:1] in tuple(_GLUE_AFTER)
        task = joined + ("" if glue else " ") + rest
    task = task.strip(_WS)
    return "" if _ONLY_PUNCT.fullmatch(task) else task


def detect(text: str, skip: Iterable[tuple[int, int]] = ()) -> WakeMatch | None:
    """The wake word in ``text`` and the task that is left, or None (see the module docstring for the rules).

    ``skip`` holds ``(start, end)`` offsets of pasted text: a wake word in a paste (a log, a file) is not the user
    asking for a mode, and the TUI sends where its pastes went (``paste_spans``)."""
    if text.lstrip(_WS).startswith("/"):
        return None
    words = list(_WORD.finditer(text))
    if not words:
        return None  # nearly every prompt: no need to look for code
    masked = _masked(text, _merge((max(0, a), min(len(text), b)) for a, b in skip))
    found: list[re.Match[str]] = []
    for m in words:
        if m.start() in masked:
            continue
        if (m.start() > 0 and text[m.start() - 1] in _QUOTES) or (m.end() < len(text) and text[m.end()] in _QUOTES):
            continue
        if (m.start() > 0 and _glued(text[m.start() - 1])) or (m.end() < len(text) and _glued(text[m.end()])):
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


def detect_enabled(config: Any, text: str, skip: Iterable[tuple[int, int]] = ()) -> WakeMatch | None:
    """:func:`detect`, unless the config switched wake words (or the mode it found) off."""
    cfg = wake_cfg(config)
    if not cfg["enabled"]:
        return None
    hit = detect(text, skip)
    return hit if hit is not None and cfg[hit.mode] else None
