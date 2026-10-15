"""What ``edit`` says when old_string is not in the file: the closest region, numbered as ``read`` numbers it.

A bare "Could not find a match" sent the model back to re-read the file (one more round trip) or to guess again.
The region of the file most like old_string, shown with the same ``number<TAB>line`` prefix as ``read``, is what the
model copies into its next old_string; a hint names the usual cause when the difference is only whitespace.
"""

from __future__ import annotations

from difflib import SequenceMatcher

#: how many file lines around the closest region are shown with it
CONTEXT_LINES = 2
#: below this similarity the file holds nothing like old_string: no region is shown
MIN_RATIO = 0.5
#: cat -n: the line number right-aligned in 6 columns, then a tab (as tools._numbered_read)
NUMBER_WIDTH = 6
#: windows scored at most: old_string is compared against every window of the file, and the model may send a
#: 20-line old_string against a 20k-line file
MAX_WINDOWS = 20_000


def _lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def closest_region(old_string: str, content: str) -> tuple[int, int, float] | None:
    """``(first line, last line, ratio)`` (1-based, inclusive) of the window of ``content`` most like ``old_string``,
    or None when nothing reaches MIN_RATIO. Windows have as many lines as old_string; each is scored on its stripped
    lines (so indentation drift still finds the region) and ties go to the earliest."""
    old = [ln.strip() for ln in _lines(old_string)]
    lines = _lines(content)
    if not old or not any(old):
        return None
    wanted = "\n".join(ln for ln in old if ln)  # blank lines are left out of the score on both sides
    stripped = [ln.strip() for ln in lines]
    anchor = next(ln for ln in old if ln)
    best: tuple[float, int, int] | None = None
    # a window as long as old_string, and one line shorter or longer: the model drops or adds a blank line
    for n in sorted(size for size in {len(old), len(old) - 1, len(old) + 1} if 1 <= size <= len(lines)):
        candidates: range | list[int] = range(len(lines) - n + 1)
        if len(candidates) > MAX_WINDOWS:
            # only windows whose first line resembles old_string's first non-blank one: the full scan is quadratic
            candidates = [
                i for i in candidates if SequenceMatcher(None, anchor, stripped[i]).quick_ratio() >= MIN_RATIO
            ][:MAX_WINDOWS]
        for i in candidates:
            window = "\n".join(ln for ln in stripped[i : i + n] if ln)
            m = SequenceMatcher(None, wanted, window)
            if m.real_quick_ratio() < MIN_RATIO or m.quick_ratio() < MIN_RATIO:
                continue
            ratio = m.ratio()
            if best is None or ratio > best[0]:
                best = (ratio, i, n)
    if best is None or best[0] < MIN_RATIO:
        return None
    return best[1] + 1, best[1] + best[2], best[0]


def _show(ch: str) -> str:
    return "tab" if ch == "\t" else "space"


def _whitespace_hint(old_string: str, region: str) -> str:
    """Why old_string missed a region that matches it stripped, named from what differs, or ""."""
    old, got = _lines(old_string), _lines(region)
    if [ln.strip() for ln in old] != [ln.strip() for ln in got]:
        if [ln.strip() for ln in old if ln.strip()] == [ln.strip() for ln in got if ln.strip()]:
            return "blank lines differ"
        return ""
    if "\r" in old_string and "\r" not in region:
        return "old_string has CRLF line ends; the file has LF"
    reasons: list[str] = []
    for a, b in zip(old, got, strict=True):
        if not a.strip():
            continue
        ia, ib = a[: len(a) - len(a.lstrip())], b[: len(b) - len(b.lstrip())]
        if ia != ib:
            kinds = {_show(c) for c in ia + ib}
            if ("\t" in ia) != ("\t" in ib) and len(kinds) > 1:
                reasons.append("the file indents with " + ("tabs" if "\t" in ib else "spaces"))
            else:
                reasons.append(f"indentation differs ({len(ia)} vs {len(ib)} {'/'.join(sorted(kinds))}s in the file)")
            break
    if not reasons and any(a != b for a, b in zip(old, got, strict=True)):
        reasons.append("trailing whitespace differs")
    return "; ".join(dict.fromkeys(reasons))


def numbered(lines: list[str], first: int) -> str:
    """``lines`` with the ``read`` tool's number prefix, starting at line ``first``."""
    return "\n".join(f"{n:>{NUMBER_WIDTH}}\t{line}" for n, line in enumerate(lines, first))


def no_match_hint(old_string: str, content: str) -> str:
    """The text appended to "Could not find a match": the closest region, numbered, and the whitespace hint."""
    found = closest_region(old_string, content)
    if found is None:
        return "\nThe file holds nothing like it: read the file (or grep for a line of it) and copy the text exactly."
    first, last, ratio = found
    lines = _lines(content)
    lo, hi = max(1, first - CONTEXT_LINES), min(len(lines), last + CONTEXT_LINES)
    where = f"line {first}" if first == last else f"lines {first}-{last}"
    region = "\n".join(lines[first - 1 : last])
    out = [f"\nClosest match at {where} ({ratio:.0%} similar), shown as read shows it (number, tab, line):"]
    out.append(numbered(lines[lo - 1 : hi], lo))
    why = _whitespace_hint(old_string, region)
    if why:
        out.append(f"Only whitespace differs: {why}. Copy the lines above exactly, without the number and tab.")
    else:
        out.append("Copy old_string from these lines exactly (without the number and tab), or widen it.")
    return "\n".join(out)
