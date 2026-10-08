# Vendored from anomalyco/opencode@ecc4916b5a9608c30e6dd58a67f2137b594407ca:
# packages/opencode/src/util/wildcard.ts (MIT). Ported to Python.
"""Wildcard pattern matching (opencode-style rules).

``*`` spans any run of chars, ``?`` is one char. A trailing ``" *"``
(space+star) is optional so ``"ls *"`` matches both ``"ls"`` and
``"ls -la"`` but not ``"lstmeval"``. Backslashes normalize to slashes.
"""

from __future__ import annotations

import re


def match(value: str, pattern: str) -> bool:
    """True if ``value`` matches ``pattern``."""
    value = value.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    escaped = re.sub(r"[.+^${}()|\[\]\\]", r"\\\g<0>", pattern)
    escaped = escaped.replace("*", ".*").replace("?", ".")
    if escaped.endswith(" .*"):
        escaped = escaped[:-3] + "( .*)?"
    return re.compile("^" + escaped + "$", re.DOTALL).search(value) is not None


def _sort_key(item: tuple[str, object]) -> tuple[int, str]:
    return (len(item[0]), item[0])


def all_matches(value: str, patterns: dict[str, object]) -> object | None:
    """Return the value of the most specific matching pattern (longest wins).

    Mirrors opencode's ``all()``: sorted by (length, key) ascending, last
    match wins.
    """
    result = None
    for pattern, rule in sorted(patterns.items(), key=_sort_key):
        if match(value, pattern):
            result = rule
    return result


def all_structured(head_tail: tuple[str, list[str]], patterns: dict[str, object]) -> object | None:
    """Match ``(head, tail)`` against multi-word patterns.

    The first pattern word must match ``head``; remaining words must match a
    subsequence of ``tail`` in order (``"*"`` skips remaining words).
    """
    head, tail = head_tail
    result = None
    for pattern, rule in sorted(patterns.items(), key=_sort_key):
        parts = pattern.split()
        if not match(head, parts[0]):
            continue
        if len(parts) == 1 or _match_sequence(tail, parts[1:]):
            result = rule
    return result


def _match_sequence(items: list[str], patterns: list[str]) -> bool:
    if not patterns:
        return True
    first, rest = patterns[0], patterns[1:]
    if first == "*":
        return _match_sequence(items, rest)
    return any(
        match(item, first) and _match_sequence(items[i + 1 :], rest) for i, item in enumerate(items)
    )
