"""Tests for the permission rule engine (evaluate/from_config/expand)."""

from __future__ import annotations

from k3code.permissions import rules


def _r(tool: str, pattern: str, action: str) -> rules.Rule:
    return rules.Rule(tool=tool, pattern=pattern, action=action)  # type: ignore[arg-type]


def test_default_when_nothing_matches() -> None:
    assert rules.evaluate("bash", "rm -rf x", []).action == "ask"
    assert rules.evaluate("bash", "rm -rf x", [], default="deny").action == "deny"


def test_simple_match() -> None:
    rs = [_r("bash", "git status*", "allow")]
    assert rules.evaluate("bash", "git status -s", rs).action == "allow"
    assert rules.evaluate("bash", "git push", rs).action == "ask"


def test_most_specific_wins() -> None:
    rs = [_r("bash", "git*", "deny"), _r("bash", "git status*", "allow")]
    assert rules.evaluate("bash", "git status -s", rs).action == "allow"
    rs_rev = [_r("bash", "git status*", "allow"), _r("bash", "git*", "deny")]
    assert rules.evaluate("bash", "git status -s", rs_rev).action == "allow"


def test_deny_beats_allow_at_same_specificity() -> None:
    rs = [_r("bash", "git push*", "allow"), _r("bash", "git push*", "deny")]
    assert rules.evaluate("bash", "git push origin", rs).action == "deny"


def test_later_ruleset_wins_same_action() -> None:
    rs = [_r("bash", "git push*", "ask"), _r("bash", "git push*", "ask")]
    assert rules.evaluate("bash", "git push origin", rs) is rs[1]


def test_tool_wildcard_matches() -> None:
    rs = [_r("*", "*", "ask")]
    assert rules.evaluate("anything", "whatever", rs).action == "ask"


def test_tool_name_is_matched_as_pattern() -> None:
    rs = [_r("read", "*", "deny")]
    assert rules.evaluate("read", "/etc/passwd", rs).action == "deny"
    assert rules.evaluate("write", "/etc/passwd", rs).action == "ask"


def test_invalid_action_rejected() -> None:
    try:
        _r("bash", "*", "sometimes")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_from_config_string_and_object() -> None:
    parsed = rules.from_config({"bash": "ask", "edit": {"/a/**": "allow"}})
    assert parsed == [_r("bash", "*", "ask"), _r("edit", "/a/**", "allow")]


def test_merge_preserves_order() -> None:
    a = [_r("bash", "*", "ask")]
    b = [_r("bash", "git*", "allow")]
    assert rules.merge(a, b) == a + b


def test_expand_tilde_and_home() -> None:
    assert rules.expand("~") == rules.expand("$HOME")
    assert rules.expand("~/docs/*").endswith("/docs/*")
    assert rules.expand("$HOME/docs/*").endswith("/docs/*")
    assert rules.expand("docs/*") == "docs/*"
    assert rules.expand("~/x") != "~/x"
    assert rules.expand("/abs/*") == "/abs/*"


def test_from_config_expands_paths() -> None:
    parsed = rules.from_config({"edit": {"~/proj/**": "allow"}})
    assert len(parsed) == 1
    assert parsed[0].pattern.endswith("/proj/**")
