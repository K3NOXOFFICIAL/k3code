"""A failed edit of a very large file is not spent on the line-by-line similarity guesses (seconds a call)."""

from __future__ import annotations

from k3code.tools.fuzzy_match import SIMILARITY_MAX_CHARS, fuzzy_find_and_replace

_BLOCK = ["def target():", "    first = 1", "    second = 2", "    third = 3", "    return first"]
# the first and last lines are right, the middle is a little off: only a similarity strategy would call it a match
_ASKED = "\n".join(["def target():", "    first = 1", "    secnd = 2", "    third = 3", "    return first"])


def _file(extra_lines: int) -> str:
    return "\n".join([*_BLOCK, *(f"filler_{i} = {i}  # padding to make the file large" for i in range(extra_lines))])


def test_a_small_file_still_gets_the_similarity_match() -> None:
    content = _file(10)
    assert len(content) < SIMILARITY_MAX_CHARS
    _, count, strategy, error = fuzzy_find_and_replace(content, _ASKED, "x = 1")
    assert error is None and count == 1 and strategy in {"block_anchor", "context_aware"}


def test_a_file_past_the_limit_gets_no_similarity_match() -> None:
    content = _file(25_000)
    assert len(content) > SIMILARITY_MAX_CHARS
    _, count, strategy, error = fuzzy_find_and_replace(content, _ASKED, "x = 1")
    assert count == 0 and strategy is None and error is not None


def test_an_exact_match_in_a_huge_file_still_works() -> None:
    content = _file(25_000)
    new, count, strategy, error = fuzzy_find_and_replace(content, "    second = 2", "    second = 22")
    assert error is None and count == 1 and strategy == "exact" and "second = 22" in new
