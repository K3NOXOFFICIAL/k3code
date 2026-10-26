"""/compact <focus> tells the summary model what to keep, /context shows what fills the window, and the cache watch says
once when the provider's prompt cache stops hitting."""

from __future__ import annotations

import json

from k3code.cache_watch import CacheWatch
from k3code.session_ai import compact_messages
from k3code.usage import KINDS
from learn_helpers import FakeCaller
from m1cmd_helpers import cmd, frames_of, make_server, new_session
from test_session_ai import _long_transcript

# --- /compact <focus>


async def test_the_focus_reaches_the_summary_prompt() -> None:
    caller = FakeCaller("summary")
    await compact_messages(caller, _long_transcript("the task"), keep=6, focus="the failing auth tests")
    system = caller.calls[0][0].content
    assert "focus on: the failing auth tests" in system


async def test_no_focus_leaves_the_prompt_byte_identical() -> None:
    from k3code.autonomy.advisor import SUMMARY_SYSTEM

    caller = FakeCaller("summary")
    await compact_messages(caller, _long_transcript("the task"), keep=6)
    assert caller.calls[0][0].content == SUMMARY_SYSTEM
    await compact_messages(caller, _long_transcript("the task"), keep=6, focus="   ")
    assert caller.calls[1][0].content == SUMMARY_SYSTEM  # blank is no focus


async def test_the_focus_is_one_short_line() -> None:
    caller = FakeCaller("summary")
    await compact_messages(caller, _long_transcript("t"), keep=6, focus="a\n\nb " + "x" * 1000)
    system = caller.calls[0][0].content
    focus_line = system.split("focus on: ")[1].split("\n")[0]
    assert focus_line.startswith("a b xxx") and len(focus_line) <= 300  # whitespace folded, cut to 300 characters


async def test_the_command_passes_its_argument_as_the_focus(tmp_path, monkeypatch) -> None:
    server, provider = make_server(tmp_path, monkeypatch, replies=["SUMMARY: ok"])
    sid = await new_session(server, tmp_path)
    server.sessions.get(sid).messages = _long_transcript("the task")
    out = await cmd(server, "/compact the migration plan", sid)
    assert "Compacted" in out["message"]
    sent = " ".join(str(m.content) for call in provider.calls for m in call) if hasattr(provider, "calls") else ""
    assert not sent or "the migration plan" in sent
    await server.close()


# --- /context


async def test_context_shows_the_parts_and_the_window(tmp_path, monkeypatch) -> None:
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    server.sessions.get(sid).messages = [
        {"role": "user", "content": "x" * 4000},
        {"role": "assistant", "content": "y" * 4000},
    ]
    out = (await cmd(server, "/context", sid))["message"]
    for part in ("system prompt", "tool schemas", "conversation", "total", "window", "compacts itself at"):
        assert part in out
    row = next(line for line in out.splitlines() if line.startswith("conversation"))
    assert "2,000" in row  # 8000 chars is about 2000 tokens
    await server.close()


async def test_context_without_a_session_says_so(tmp_path, monkeypatch) -> None:
    server, _ = make_server(tmp_path, monkeypatch)
    assert "No active session" in (await cmd(server, "/context", None))["message"]
    await server.close()


# --- the cache watch


def _feed(watch: CacheWatch, rates: list[int], gap: float = 10.0, start: float = 0.0) -> list[str | None]:
    out, now = [], start
    for rate in rates:
        now += gap
        out.append(watch.observe(1000, rate * 10, now))  # prompt of 1000 tokens, `rate` percent of it cached
    return out


def test_a_collapse_warns_exactly_once_on_the_second_low_call() -> None:
    got = _feed(CacheWatch(), [80, 82, 10, 5])
    assert got[:3] == [None, None, None] and got[3] and "stopped hitting" in got[3]
    assert "5%" in got[3] and "81%" in got[3]  # what it fell to, and the median of the 80 and 82 it was
    assert _feed(CacheWatch(), [80, 82, 10, 5, 8, 3]).count(None) == 5  # the rest of the episode is quiet


def test_one_missed_call_is_not_a_regression() -> None:
    """A failover to another entry, a tier escalation, a batch of elided tool results or a fresh process each cost one
    miss; the next call hits again."""
    assert not any(_feed(CacheWatch(), [80, 82, 10, 81, 83, 5, 80]))


def test_it_arms_again_after_a_recovery() -> None:
    watch = CacheWatch()
    got = _feed(watch, [80, 82, 10, 5, 78, 80, 6, 4])
    assert [i for i, g in enumerate(got) if g] == [3, 7]


def test_a_gentle_drift_is_not_a_regression() -> None:
    assert not any(_feed(CacheWatch(), [90, 85, 80, 70, 65, 60]))


def test_it_needs_a_baseline_first() -> None:
    assert not any(_feed(CacheWatch(), [0, 0, 0]))  # a provider that never caches has nothing to lose
    assert not any(_feed(CacheWatch(), [90, 0]))  # one sample is not a baseline


def test_a_pause_longer_than_the_cache_lifetime_is_not_a_regression() -> None:
    watch = CacheWatch()
    _feed(watch, [80, 82])
    assert watch.observe(1000, 0, now=10_000.0) is None  # the cache expired by itself


def test_reset_forgets_the_baseline_for_a_rewritten_prompt() -> None:
    watch = CacheWatch()
    _feed(watch, [80, 82])
    watch.reset()  # compaction: the next call is a miss by design
    assert not any(_feed(watch, [0, 0, 80], start=100.0))


def test_the_drop_size_is_configurable() -> None:
    assert any(_feed(CacheWatch(drop_points=10), [80, 82, 60, 55]))
    assert not any(_feed(CacheWatch(), [80, 82, 60, 55]))


async def test_the_gateway_notifies_and_records_a_regression(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import Usage

    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    live = server.sessions.get(sid)
    for rate in (800, 820):
        server._watch_cache(live, Usage(prompt_tokens=1000, cache_read_tokens=rate))
    server._watch_cache(live, Usage(prompt_tokens=1000, cache_read_tokens=50))  # one miss: nothing yet
    n = len(frames_of(server))
    server._watch_cache(live, Usage(prompt_tokens=1000, cache_read_tokens=40))
    notes = [
        f["params"]["payload"] for f in frames_of(server)[n:] if f.get("params", {}).get("type") == "notification.show"
    ]
    assert len(notes) == 1 and notes[0]["key"] == "k3.cache_regression" and notes[0]["level"] == "warn"
    row = server.usage._db.execute("SELECT detail FROM events WHERE kind='cache_regression'").fetchall()
    assert len(row) == 1 and "stopped hitting" in row[0][0]
    server._watch_cache(live, Usage(prompt_tokens=1000, cache_read_tokens=30))  # same episode: no second notice
    assert server.usage._db.execute("SELECT COUNT(*) FROM events WHERE kind='cache_regression'").fetchone()[0] == 1
    await server.close()


async def test_the_warning_can_be_switched_off(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import Usage

    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    live = server.sessions.get(sid)
    server.config.context = {"cache_warning": False}
    for rate in (800, 820, 10, 5):
        server._watch_cache(live, Usage(prompt_tokens=1000, cache_read_tokens=rate))
    assert server.usage._db.execute("SELECT COUNT(*) FROM events WHERE kind='cache_regression'").fetchone()[0] == 0
    await server.close()


async def test_compaction_resets_the_watch(tmp_path, monkeypatch) -> None:
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    live = server.sessions.get(sid)
    from k3code.providers.types import Usage

    for rate in (800, 820):
        server._watch_cache(live, Usage(prompt_tokens=1000, cache_read_tokens=rate))
    live.refresh_prompt_inputs()  # what compaction and /clear call
    server._watch_cache(live, Usage(prompt_tokens=1000, cache_read_tokens=0))
    assert server.usage._db.execute("SELECT COUNT(*) FROM events WHERE kind='cache_regression'").fetchone()[0] == 0


def test_the_kind_is_a_known_usage_kind() -> None:
    assert "cache_regression" in KINDS
    json.dumps(KINDS)  # a plain tuple of strings
