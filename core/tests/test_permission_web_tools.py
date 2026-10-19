"""web_search, web_fetch and web_browse read the web and change nothing: a public host never asks, in any interactive
mode; a private, loopback or local host, a URL with credentials or one that is not http(s) asks a person."""

from __future__ import annotations

import pytest

from k3code.permissions import Rule, decide, suggest_rules

CWD = "/proj"
MODES = ("default", "accept-edits", "plan", "auto")
PUBLIC = "https://docs.python.org/3/library/os.html"


def _r(tool: str, pattern: str, action: str) -> Rule:
    return Rule(tool=tool, pattern=pattern, action=action)  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", MODES)
def test_web_search_never_asks(mode) -> None:
    d = decide(mode=mode, tool="web_search", args={"query": "python asyncio taskgroup"}, cwd=CWD)
    assert d.action == "allow", d
    assert decide(mode=mode, tool="web_search", args={"query": "x"}, cwd=CWD, headless=True).action == "allow"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", ["web_fetch", "web_browse"])
@pytest.mark.parametrize(
    "url",
    [PUBLIC, "http://example.com", "https://8.8.8.8/", "https://[2606:4700:4700::1111]/", "https://bücher.de/x"],
)
def test_a_public_host_never_asks(mode, tool, url) -> None:
    d = decide(mode=mode, tool=tool, args={"url": url}, cwd=CWD)
    assert d.action == "allow" and not d.auto_allowed, d
    assert decide(mode=mode, tool=tool, args={"url": url}, cwd=CWD, headless=True).action == "allow"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "http://127.1/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.0.0.5/",
        "http://172.16.3.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fc00::1]/",
        "http://[fe80::1%25eth0]/",
        "http://localhost:3000/",
        "http://LOCALHOST./",
        "http://intranet/",
        "http://printer.local/",
        "http://metadata.google.internal/",
        "http://app.localhost/",
        "https://user:pw@example.com/",
        "https://docs.python.org@evil.example/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "http:///x",
        "http://*/",
        "http://exa mple.com/",
        "",
    ],
)
@pytest.mark.parametrize("mode", MODES)
def test_a_private_local_or_odd_url_needs_a_person(mode, url) -> None:
    for tool in ("web_fetch", "web_browse"):
        d = decide(mode=mode, tool=tool, args={"url": url}, cwd=CWD)
        assert d.action == "ask" and d.needs_human and not d.always_ask, (tool, d)
        assert "pw" not in (d.message or ""), d  # the URL may carry a password: it is not echoed
        d = decide(mode=mode, tool=tool, args={"url": url}, cwd=CWD, headless=True)
        assert d.action == "deny", (tool, d)


def test_yolo_still_reads_a_private_host() -> None:
    assert decide(mode="yolo", tool="web_fetch", args={"url": "http://127.0.0.1/"}, cwd=CWD).action == "allow"


@pytest.mark.parametrize("tool", ["web_fetch", "web_browse", "web_search"])
@pytest.mark.parametrize("layer", ["user_rules", "project_rules", "session_rules"])
def test_a_rule_still_wins_over_the_default(tool, layer) -> None:
    args = {"query": "x"} if tool == "web_search" else {"url": PUBLIC}
    for mode in MODES:
        d = decide(mode=mode, tool=tool, args=args, cwd=CWD, **{layer: [_r(tool, "*", "deny")]})
        assert d.action == "deny", (mode, d)
        d = decide(mode="accept-edits", tool=tool, args=args, cwd=CWD, **{layer: [_r(tool, "*", "ask")]})
        assert d.action == "ask", d


def test_a_rule_is_matched_against_the_host() -> None:
    user = [_r("web_fetch", "*.python.org", "deny")]
    assert decide(mode="auto", tool="web_fetch", args={"url": PUBLIC}, cwd=CWD, user_rules=user).action == "deny"
    d = decide(mode="auto", tool="web_fetch", args={"url": "https://example.com/"}, cwd=CWD, user_rules=user)
    assert d.action == "allow", d


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://10.0.0.5:8080/admin", "10.0.0.5"),
        ("http://localhost:3000/", "localhost"),
        ("http://intranet/", "intranet"),
    ],
)
def test_an_always_answer_stores_the_host_and_matches_next_time(url, host) -> None:
    d = decide(mode="accept-edits", tool="web_fetch", args={"url": url}, cwd=CWD)
    assert d.action == "ask" and d.patterns == [host], d
    rules = suggest_rules("web_fetch", d)
    assert rules == [_r("web_fetch", host, "allow")]
    d = decide(mode="accept-edits", tool="web_fetch", args={"url": url}, cwd=CWD, session_rules=rules)
    assert d.action == "allow", d
    # the stored host covers that host only
    d = decide(mode="accept-edits", tool="web_fetch", args={"url": "http://10.0.0.6/"}, cwd=CWD, session_rules=rules)
    assert d.action == "ask", d


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://*/", "http:///x", "not a url"])
def test_an_always_answer_on_a_url_without_a_usable_host_stores_nothing(url) -> None:
    d = decide(mode="accept-edits", tool="web_fetch", args={"url": url}, cwd=CWD)
    assert d.action == "ask" and d.patterns == [], d
    assert suggest_rules("web_fetch", d) == []


def test_an_mcp_tool_named_like_a_web_tool_keeps_the_generic_decision() -> None:
    for tool in ("mcp__hub__web_fetch", "mcp__hub__web_search"):
        assert decide(mode="accept-edits", tool=tool, args={"url": PUBLIC}, cwd=CWD).action == "ask"
        assert decide(mode="plan", tool=tool, args={"url": PUBLIC}, cwd=CWD).action == "deny"
