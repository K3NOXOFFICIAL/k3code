"""Compaction on the conversations that used to defeat it: one long task, a huge kept tail, an empty summary."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from k3code.context_budget import message_tokens, overhead_tokens, text_tokens
from k3code.gateway.server import COMPACT_RETRY_AFTER_S, _estimate_tokens
from k3code.providers.types import Message
from k3code.session_ai import (
    SUMMARY_PREFIX,
    compact_messages,
    split_between_steps,
    split_for_compaction,
)
from learn_helpers import FakeCaller
from test_auto_compaction import Recording, long_history, seed


def one_long_task(steps: int = 40) -> list[dict]:
    """A single user prompt followed by ``steps`` assistant tool calls with their results."""
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "TASK: port the parser"}]
    for i in range(steps):
        call = {"id": f"c{i}", "name": "bash", "arguments": {"command": f"step {i}"}}
        msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
        msgs.append({"role": "tool", "name": "bash", "tool_call_id": f"c{i}", "content": f"out {i} " + "x" * 200})
    return msgs


def tool_pairs_intact(msgs: list[dict]) -> bool:
    answered = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
    asked = {c["id"] for m in msgs for c in m.get("tool_calls") or []}
    return answered <= asked and all(
        not (m["role"] == "assistant" and m.get("tool_calls")) or {c["id"] for c in m["tool_calls"]} <= answered
        for m in msgs
    )


async def test_one_user_message_and_a_long_run_of_tool_calls_still_compacts():
    msgs = one_long_task()
    assert split_for_compaction(msgs, 8) == 1  # the only user message: nothing before it to fold
    cut = split_between_steps(msgs, 8)
    assert cut > 1 and msgs[cut]["role"] == "assistant" and msgs[cut - 1]["role"] == "tool"
    caller = FakeCaller("did steps 0-30")
    new, folded = await compact_messages(caller, msgs, keep=8)
    assert folded == cut
    assert new[0]["role"] == "system" and new[1]["content"] == "TASK: port the parser"
    assert new[2]["content"].startswith(SUMMARY_PREFIX)
    assert new[3]["role"] == "assistant" and tool_pairs_intact(new)
    # the summarizer saw what the tool calls were, not just their output
    assert "assistant called: bash" in caller.calls[0][1].content


async def test_a_tail_with_no_safe_cut_folds_nothing():
    msgs = one_long_task(2)  # system, user, 2 call/result pairs: keeping 8 leaves nothing to fold
    new, folded = await compact_messages(FakeCaller("x"), msgs, keep=8)
    assert folded == 0 and new == msgs


async def test_a_summary_and_a_task_statement_alone_are_not_summarized_again():
    """The last turn's tool results are the whole excess: re-summarizing the summary on every turn frees nothing."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "TASK"},
        {"role": "user", "content": SUMMARY_PREFIX + "earlier work"},
        *one_long_task(30)[2:],
    ]
    caller = FakeCaller("again")
    new, folded = await compact_messages(caller, msgs, keep=8)
    assert folded > 0  # the 30 steps after the summary are folded once...
    caller2 = FakeCaller("and again")
    again, folded2 = await compact_messages(caller2, new, keep=len(new) - 3)
    assert folded2 == 0 and again == new and caller2.calls == []  # ...but the summary alone is left


async def test_an_empty_summary_keeps_the_history():
    msgs = one_long_task()
    with pytest.raises(ValueError, match="no text"):
        await compact_messages(FakeCaller("   "), msgs, keep=8)


async def test_keep_zero_does_not_crash():
    assert split_for_compaction(long_history(5), 0) == 9  # a keep below 1 counts as 1: the last user message
    new, folded = await compact_messages(FakeCaller("s"), long_history(5), keep=0)
    assert folded > 0 and new[-1]["role"] == "assistant"


# ── the gateway around it ─────────────────────────────────────────────


class Failing(Recording):
    """The summary call fails; everything else answers."""

    def __init__(self) -> None:
        super().__init__()
        self.summary_calls = 0

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        if "Summarize this coding conversation" in str(messages[0].content):
            self.summary_calls += 1
            raise RuntimeError("summary model down")
        async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
            yield event


async def test_a_failed_compaction_is_not_tried_again_on_every_turn(tmp_path, monkeypatch):
    provider = Failing()
    server, live = await seed(tmp_path, monkeypatch, provider, context={"compact_at_tokens": 2000})
    assert await server._maybe_compact(live) == 0
    first = provider.summary_calls
    assert first >= 1 and live.compact_retry_at > time.monotonic()
    assert await server._maybe_compact(live) == 0
    assert provider.summary_calls == first  # backed off
    live.compact_retry_at = time.monotonic() - COMPACT_RETRY_AFTER_S  # the wait is over
    await server._maybe_compact(live)
    assert provider.summary_calls > first
    await server.close()


async def test_what_is_appended_during_the_summary_call_is_kept(tmp_path, monkeypatch):
    class Appending(Recording):
        live = None

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            if "Summarize this coding conversation" in str(messages[0].content):
                self.live.stored.messages.append({"role": "user", "content": "LATE steer"})
            async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
                yield event

    provider = Appending()
    server, live = await seed(tmp_path, monkeypatch, provider, context={"keep_messages": 6})
    provider.live = live
    assert await server.compact_session(live) > 0
    assert live.stored.messages[-1] == {"role": "user", "content": "LATE steer"}
    assert any(str(m["content"]).startswith(SUMMARY_PREFIX) for m in live.stored.messages)
    await server.close()


async def test_a_rewrite_during_the_summary_call_wins(tmp_path, monkeypatch):
    class Clearing(Recording):
        live = None

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            if "Summarize this coding conversation" in str(messages[0].content):
                self.live.stored.messages = []  # /clear ran meanwhile
            async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
                yield event

    provider = Clearing()
    server, live = await seed(tmp_path, monkeypatch, provider, context={"keep_messages": 6})
    provider.live = live
    assert await server.compact_session(live) == 0
    assert live.stored.messages == []  # the cleared conversation did not come back
    await server.close()


# ── the estimate ──────────────────────────────────────────────────────


def test_non_ascii_text_costs_more_than_a_quarter_token_per_character():
    assert text_tokens("a" * 4000) == 1000
    assert 10_000 <= text_tokens("漢" * 10_000) <= 15_000  # a token or so per character
    plain = "the quick brown fox jumps over the lazy dog, again and again. " * 100
    assert text_tokens(plain.replace("quick", "quïck")) <= text_tokens(plain) * 1.1  # an accent now and then


def test_the_estimate_follows_the_configured_tool_output_limit():
    msgs = [{"role": "tool", "name": "bash", "tool_call_id": "1", "content": "x" * 50_000}]
    assert _estimate_tokens(msgs) < 3_000  # the default clip: head and tail of 10k chars
    assert _estimate_tokens(msgs, 50_000) == 12_500  # what a loop with context.tool_output_chars=50000 sends


def test_the_system_prompt_is_counted_once():
    system = "s" * 40_000
    msgs = [Message(role="system", content=system), Message(role="user", content="hi")]
    assert overhead_tokens(system, []) >= 10_000
    assert message_tokens(msgs) < 10  # the request's system prompt is overhead_tokens' to count
    assert message_tokens([SimpleNamespace(role="system", content="x" * 400, tool_calls=[])]) == 0


async def test_an_overflow_that_cannot_be_compacted_keeps_the_failed_attempt(tmp_path, monkeypatch):
    """No retry happens, so the prompt and the work the turn did stay in the history instead of vanishing."""
    from k3code.providers.base import ProviderError

    class Overflowing(Recording):
        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            if "NEWPROMPT" in "\n".join(str(m.content or "") for m in messages):
                raise ProviderError(
                    "maximum context length is 1000 tokens, however you requested 9000", status_code=400
                )
            async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
                yield event

    server, live = await seed(tmp_path, monkeypatch, Overflowing(), context={"compact_at_tokens": 10_000_000})
    live.stored.messages = long_history(2)  # too short to fold anything
    status, _ = await server._run_turn(live, "NEWPROMPT please")
    assert status == "error"
    assert any(m["content"] == "NEWPROMPT please" for m in live.stored.messages)
    await server.close()
