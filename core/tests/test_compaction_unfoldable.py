"""A limit below what compaction cannot fold (the system prompt, the tool list, the kept tail and the summary) must not
cost a summary call on every turn: a session whose request is still over the limit right after a fold waits for its
history to grow, and the user is told once. Nothing else about compaction changes."""

from __future__ import annotations

import re

from k3code.context_budget import MAX_COMPACT_RATIO, compact_resume_at, context_window
from k3code.providers.base import ProviderError
from k3code.session_ai import SUMMARY_PREFIX
from m1cmd_helpers import cmd
from test_auto_compaction import Recording, long_history, seed

#: Far below the kept tail alone (40 messages of ~100 tokens), whatever the system prompt and the tools weigh.
LIMIT = 2000
KEY = "k3.compact_unfoldable"


class Counting(Recording):
    """Answers every call and counts the summary calls; with ``overflow_once`` the first turn that carries NEWPROMPT is
    rejected as too long."""

    def __init__(self, overflow_once: bool = False) -> None:
        super().__init__()
        self.summary_calls = 0
        self.overflow_once = overflow_once

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        if "Summarize this coding conversation" in str(messages[0].content):
            self.summary_calls += 1
        elif self.overflow_once and "NEWPROMPT" in "\n".join(str(m.content or "") for m in messages):
            self.overflow_once = False
            raise ProviderError("maximum context length is 1000 tokens, however you requested 9000", status_code=400)
        async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
            yield event


async def session_with_limit(tmp_path, monkeypatch, provider, *, keep: int = 40, limit: int = LIMIT):
    """A seeded session (80 messages) whose own /autocompact limit is ``limit`` tokens; ``keep`` messages are kept."""
    server, live = await seed(tmp_path, monkeypatch, provider, context={"keep_messages": keep})
    live.stored.meta["autocompact"] = {"enabled": True, "tokens": limit, "ratio": None}
    return server, live


def notices(server) -> list[dict]:
    return [
        e["payload"]
        for e in server.event_log
        if e["type"] == "notification.show" and str(e["payload"].get("key", "")).startswith(KEY)
    ]


def window_of(server, live) -> int:
    return context_window(server.config, server._active_model(live))


def grow(live, tokens: int) -> None:
    """Add about ``tokens`` of conversation to the end of the history."""
    live.stored.messages += [{"role": "user", "content": "g" * 4 * tokens}, {"role": "assistant", "content": "noted"}]


async def fold_once_and_get_stuck(tmp_path, monkeypatch, provider):
    server, live = await session_with_limit(tmp_path, monkeypatch, provider)
    status, _ = await server._run_turn(live, "turn 0")
    assert status == "done" and provider.summary_calls == 1
    assert server._unfoldable_size(live) >= LIMIT  # the fold left the request over the limit
    return server, live


# ── the rule ──────────────────────────────────────────────────────────


def test_the_next_compaction_waits_for_a_quarter_of_the_limit_and_never_past_the_window():
    assert compact_resume_at(32_000, 10_000, 200_000) == 34_500  # grown by 25% of the limit
    # the defect's 8,000-token window: 70% = 5,600, 6,899 left after the fold. 6,899 + 1,400 is past the window
    assert compact_resume_at(6_899, 5_600, 8_000) == int(8_000 * MAX_COMPACT_RATIO) == 7_600
    # already that close to the window: no wait, as before
    assert compact_resume_at(7_900, 5_600, 8_000) == 7_900


# ── the defect ────────────────────────────────────────────────────────


async def test_a_limit_below_what_compaction_leaves_summarizes_once_not_on_every_turn(tmp_path, monkeypatch):
    provider = Counting()
    server, live = await session_with_limit(tmp_path, monkeypatch, provider)
    for i in range(5):
        status, _ = await server._run_turn(live, f"turn {i}")
        assert status == "done"
        assert provider.summary_calls == 1, f"turn {i} summarized again"
    assert live.stored.meta["compactions"] == 1
    assert server._request_tokens(live) >= LIMIT  # still over the limit: it is the fold that cannot help
    await server.close()


async def test_the_user_is_told_once_that_the_limit_is_below_what_cannot_be_folded(tmp_path, monkeypatch):
    provider = Counting()
    server, live = await fold_once_and_get_stuck(tmp_path, monkeypatch, provider)
    assert len(notices(server)) == 1  # at the fold
    for i in range(3):  # the turns that wait say nothing more
        await server._run_turn(live, f"turn {i + 1}")
    (note,) = notices(server)
    assert note["kind"] == "ttl" and note["ttl_ms"] == 15000 and note["key"] == f"{KEY}.{live.session_id}"
    assert re.fullmatch(
        rf"auto-compact limit {LIMIT:,} is below what every request carries "
        r"\(about [\d,]+ tokens after compaction\): raise it with /autocompact",
        note["text"],
    ), note["text"]
    await server.close()


async def test_it_summarizes_again_once_the_history_has_grown_by_the_allowance(tmp_path, monkeypatch):
    provider = Counting()
    server, live = await fold_once_and_get_stuck(tmp_path, monkeypatch, provider)
    after = server._unfoldable_size(live)
    resume = compact_resume_at(after, LIMIT, window_of(server, live))
    assert resume == after + LIMIT // 4
    grow(live, resume - server._request_tokens(live) - 100)  # 100 tokens short of it
    await server._run_turn(live, "almost")
    assert provider.summary_calls == 1
    grow(live, 300)  # past it
    await server._run_turn(live, "grown")
    assert provider.summary_calls == 2 and live.stored.meta["compactions"] == 2
    assert len(notices(server)) == 1  # the same spell: not told again
    await server.close()


async def test_the_wait_never_goes_past_the_models_window(tmp_path, monkeypatch):
    """Waiting for the full allowance would run into the provider's rejection: the window caps it."""
    provider = Counting()
    server, live = await fold_once_and_get_stuck(tmp_path, monkeypatch, provider)
    after = server._unfoldable_size(live)
    # a window whose 95% is 100 tokens above the size left by the fold: less than the 500-token allowance
    server.config.models = {server._active_model(live): {"context_window": int((after + 100) / MAX_COMPACT_RATIO) + 1}}
    assert compact_resume_at(after, LIMIT, window_of(server, live)) < after + LIMIT // 4
    grow(live, 250)
    await server._run_turn(live, "past the window's cap")
    assert provider.summary_calls == 2
    await server.close()


async def test_a_new_spell_starts_clean_after_the_request_dropped_below_the_limit(tmp_path, monkeypatch):
    provider = Counting()
    server, live = await fold_once_and_get_stuck(tmp_path, monkeypatch, provider)
    live.stored.meta["autocompact"]["tokens"] = 20_000  # the limit raised with /autocompact --session
    assert await server._maybe_compact(live) == 0
    assert live.compact_unfoldable == 0 and not live.compact_unfoldable_noted
    live.stored.meta["autocompact"]["tokens"] = LIMIT  # and lowered again: compacts at once, no stale wait
    live.stored.messages = long_history(40)
    assert await server._maybe_compact(live) > 0
    assert provider.summary_calls == 2
    assert len(notices(server)) == 2  # a second spell: told again
    await server.close()


# ── what is never held back ───────────────────────────────────────────


async def test_slash_compact_still_compacts_while_automatic_compaction_waits(tmp_path, monkeypatch):
    provider = Counting()
    server, live = await fold_once_and_get_stuck(tmp_path, monkeypatch, provider)
    await server._run_turn(live, "turn 1")
    assert provider.summary_calls == 1  # waiting
    res = await cmd(server, "/compact", live.session_id)
    assert "Compacted" in str(res), res
    assert provider.summary_calls == 2
    assert server._unfoldable_size(live) >= LIMIT  # it armed the wait again: the next turn does not summarize
    await server._run_turn(live, "turn 2")
    assert provider.summary_calls == 2
    await server.close()


async def test_a_forced_compaction_is_never_held_back(tmp_path, monkeypatch):
    provider = Counting()
    server, live = await fold_once_and_get_stuck(tmp_path, monkeypatch, provider)
    await server._run_turn(live, "turn 1")
    assert provider.summary_calls == 1  # waiting
    assert await server._maybe_compact(live, force=True) > 0
    assert provider.summary_calls == 2
    await server.close()


async def test_a_context_overflow_still_compacts_and_retries_while_automatic_compaction_waits(tmp_path, monkeypatch):
    provider = Counting()
    server, live = await fold_once_and_get_stuck(tmp_path, monkeypatch, provider)
    provider.overflow_once = True  # the provider now rejects the next turn as too long
    status, _ = await server._run_turn(live, "NEWPROMPT please")
    assert status == "done"
    assert provider.summary_calls == 2  # the recovery summarized although the wait was on
    prompts = [m["content"] for m in live.stored.messages if m["content"] == "NEWPROMPT please"]
    assert prompts == ["NEWPROMPT please"] and live.stored.messages[-1]["content"] == "ok"
    await server.close()


# ── the ordinary case ─────────────────────────────────────────────────


async def test_a_fold_that_gets_the_request_below_the_limit_is_as_before(tmp_path, monkeypatch):
    provider = Counting()
    big = 20_000  # ~10k tokens of history and the prompt: over a 20,000 limit only with 240 messages
    server, live = await session_with_limit(tmp_path, monkeypatch, provider, keep=6, limit=big)
    live.stored.messages = long_history(120)
    assert server._request_tokens(live) > big
    status, _ = await server._run_turn(live, "next request")
    assert status == "done" and provider.summary_calls == 1
    msgs = live.stored.messages
    assert msgs[2]["content"].startswith(SUMMARY_PREFIX) and len(msgs) < 20
    assert live.compact_unfoldable == 0 and notices(server) == []
    assert server.context_fields(live)["compressions"] == 1 and live.stored.meta["compactions"] == 1
    # the same events as ever: the status pair and the one "compacted" notice
    kinds = [e["payload"].get("kind") for e in server.event_log if e["type"] == "status.update"]
    assert kinds.count("compacting") == 1 and kinds.count("compacted") == 1
    compacted = [
        e["payload"]
        for e in server.event_log
        if e["type"] == "notification.show" and e["payload"]["key"] == "k3.compact"
    ]
    assert len(compacted) == 1 and compacted[0]["text"].startswith("Context compacted: ")
    assert compacted[0]["kind"] == "ttl" and compacted[0]["ttl_ms"] == 15000  # it goes away by itself
    # and it compacts again as soon as the history is over the limit once more
    live.stored.messages += long_history(120)[1:]
    await server._run_turn(live, "again")
    assert provider.summary_calls == 2 and notices(server) == []
    await server.close()
