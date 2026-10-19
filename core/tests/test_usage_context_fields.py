"""Cache-hit % and context usage reach the TUI: the keys it reads in the usage payload and in ``session.usage``."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3code.gateway.server import _session_usage, _usage_payload
from k3code.providers.types import Usage


def test_cache_hit_pct_is_the_integer_share_of_the_prompt_read_from_cache():
    assert _usage_payload(Usage(prompt_tokens=1000, cache_read_tokens=800))["cache_hit_pct"] == 80
    assert _usage_payload(Usage(prompt_tokens=3, cache_read_tokens=1))["cache_hit_pct"] == 33


def test_cache_hit_pct_is_absent_without_cache_reads_or_a_prompt():
    assert "cache_hit_pct" not in _usage_payload(Usage(prompt_tokens=1000, cache_read_tokens=0))
    assert "cache_hit_pct" not in _usage_payload(Usage(prompt_tokens=0, cache_read_tokens=500))


def test_context_fields_come_from_the_request_prompt_and_the_window():
    p = _usage_payload(Usage(prompt_tokens=50_000, completion_tokens=10), 200_000)
    assert (p["context_used"], p["context_max"], p["context_percent"]) == (50_000, 200_000, 25)
    assert p["total_tokens"] == 50_010  # the existing keys stay


def test_context_percent_is_capped_but_used_is_not_clamped():
    p = _usage_payload(Usage(prompt_tokens=300_000), 200_000)
    assert p["context_used"] == 300_000 and p["context_percent"] == 100


def test_context_fields_are_absent_without_a_prompt_or_a_window():
    assert "context_used" not in _usage_payload(Usage(prompt_tokens=0), 200_000)
    p = _usage_payload(Usage(prompt_tokens=100))
    assert not {"context_used", "context_max", "context_percent"} & p.keys()


def _server(session, *, estimate=1234, window=200_000):
    usage = SimpleNamespace(aggregate=lambda **_: [{"tokens_in": 10, "tokens_out": 5, "calls": 2}])
    return SimpleNamespace(
        _session_for=lambda _sid: session,
        usage=usage,
        config=SimpleNamespace(default_model="m"),
        _request_tokens=lambda _s: estimate,
        _context_max=lambda _s: window,
    )


def _session(last_prompt):
    return SimpleNamespace(session_id="s", stored=SimpleNamespace(model="m"), last_prompt_tokens=last_prompt)


async def test_session_usage_uses_the_reported_prompt_when_there_is_one():
    r = await _session_usage(_server(_session(50_000)), {})
    assert (r["context_used"], r["context_max"], r["context_percent"]) == (50_000, 200_000, 25)
    assert r["context_estimated"] is False
    assert (r["input"], r["output"], r["total"], r["calls"], r["model"]) == (10, 5, 15, 2, "m")


async def test_session_usage_estimates_when_nothing_was_reported():
    r = await _session_usage(_server(_session(0), estimate=20_000), {})
    assert (r["context_used"], r["context_percent"], r["context_estimated"]) == (20_000, 10, True)


async def test_session_usage_omits_context_when_nothing_is_known():
    r = await _session_usage(_server(_session(0), estimate=0), {})
    assert not {"context_used", "context_max", "context_percent", "context_estimated"} & r.keys()


async def test_session_usage_without_a_session_is_still_just_calls_zero():
    server = SimpleNamespace(_session_for=lambda _sid: None)
    assert await _session_usage(server, {}) == {"calls": 0}


def test_key_names_match_what_the_tui_reads():
    types = Path(__file__).resolve().parents[2] / "tui" / "src" / "gatewayTypes.ts"
    if not types.is_file():
        pytest.skip("tui/ not checked out next to core/")
    text = types.read_text()
    p = _usage_payload(Usage(prompt_tokens=1000, cache_read_tokens=800), 10_000)
    for key in ("cache_hit_pct", "context_used", "context_max", "context_percent"):
        assert re.search(rf"\b{key}\??:", text), key
        assert key in p, key
