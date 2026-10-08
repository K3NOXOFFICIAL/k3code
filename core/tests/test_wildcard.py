"""Tests for wildcard matching (ported opencode semantics)."""

from __future__ import annotations

import pytest

from k3code.permissions import wildcard


def test_exact() -> None:
    assert wildcard.match("foo", "foo")
    assert not wildcard.match("foobar", "foo")


def test_star_spans_anything() -> None:
    assert wildcard.match("foo", "*")
    assert wildcard.match("foo/bar/baz", "foo*")
    assert wildcard.match("foo", "foo*")
    assert wildcard.match("foo/", "foo*")


def test_question_mark() -> None:
    assert wildcard.match("foo", "fo?")
    assert not wildcard.match("foobar", "fo?")
    assert wildcard.match("a.c", "a?c")


@pytest.mark.parametrize(
    ("value", "pattern", "expected"),
    [
        ("ls", "ls *", True),
        ("ls -la", "ls *", True),
        ("lstmeval", "ls *", False),
        ("git commit -m 'x'", "git commit *", True),
        ("git commit", "git commit *", True),
        ("git commitx", "git commit *", False),
    ],
)
def test_trailing_space_star_is_optional(value: str, pattern: str, expected: bool) -> None:
    assert wildcard.match(value, pattern) is expected


def test_backslash_normalizes_to_slash() -> None:
    assert wildcard.match("foo\\bar", "foo/bar")
    assert wildcard.match("foo/bar", "foo\\bar")


def test_special_chars_escaped() -> None:
    assert wildcard.match("a.c", "a.c")
    assert not wildcard.match("abc", "a.c")
    assert wildcard.match("a+b", "a+b")


def test_all_most_specific_wins() -> None:
    rules = {"*": "ask", "git status*": "allow", "git*": "ask"}
    assert wildcard.all_matches("git status -s", rules) == "allow"
    assert wildcard.all_matches("git push", rules) == "ask"
    assert wildcard.all_matches("ls -la", rules) == "ask"


def test_all_no_match_returns_none() -> None:
    assert wildcard.all_matches("python x.py", {"git*": "allow"}) is None


def test_all_length_then_key_order() -> None:
    rules = {"*": "a", "ab*": "b", "abc*": "c"}
    assert wildcard.all_matches("abcd", rules) == "c"
    assert wildcard.all_matches("abx", rules) == "b"


def test_all_structured_head_tail() -> None:
    rules = {
        "git commit *": "allow",
        "git *": "ask",
        "find *": "allow",
        "find * -delete*": "ask",
        "sort*": "allow",
        "sort -o *": "ask",
    }
    assert wildcard.all_structured(("git", ["commit", "-m", "x"]), rules) == "allow"
    assert wildcard.all_structured(("git", ["push"]), rules) == "ask"
    assert wildcard.all_structured(("find", ["src", "-delete"]), rules) == "ask"
    assert wildcard.all_structured(("find", ["src", "-print"]), rules) == "allow"
    assert wildcard.all_structured(("sort", ["-o", "out.txt"]), rules) == "ask"
    assert wildcard.all_structured(("sort", ["--reverse"]), rules) == "allow"
    assert wildcard.all_structured(("python", ["x.py"]), rules) is None


def test_all_structured_star_skips() -> None:
    rules = {"git commit --amend *": "ask"}
    assert wildcard.all_structured(("git", ["commit", "--amend"]), rules) == "ask"
    assert wildcard.all_structured(("git", ["commit", "--amend", "--no-edit"]), rules) == "ask"
    assert wildcard.all_structured(("git", ["commit"]), rules) is None
