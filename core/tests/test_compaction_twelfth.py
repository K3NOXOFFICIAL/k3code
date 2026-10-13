"""Compaction and overflow recovery in the gateway (twelfth pass): a long single turn that overflows the window is
folded and goes on, the cut that frees the most is taken, a prompt is only clipped when it cannot fit, ..."""

from __future__ import annotations

import json

import pytest

from k3code.errors import ContextOverflow
from k3code.gateway.server import COMPACTED_CONTINUATION
from k3code.providers.base import ProviderError
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.router import Router, build_chain
from k3code.session_ai import SUMMARY_PREFIX, compact_messages
from learn_helpers import FakeCaller
from m1cmd_helpers import make_server, new_session
from test_auto_compaction import long_history


def _size(messages) -> int:
    return sum(len(str(m.content or "")) + sum(len(json.dumps(tc.arguments)) for tc in m.tool_calls) for m in messages)


class Climbing:
    """Reads one more file on each of its first ``steps`` calls, then answers. A request more than ``headroom``
    characters larger than the first one is rejected as too long (a summary call never is)."""

    name = "fake"
    base_url = "fake://"

    def __init__(self, root, steps: int, headroom: int) -> None:
        self.root, self.steps, self.headroom = root, steps, headroom
        self.first_size = 0
        self.calls = self.summaries = self.overflows = 0
        self.requests: list[list[Message]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.requests.append(list(messages))
        if messages and "Summarize this coding conversation" in str(messages[0].content):
            self.summaries += 1
            reply = Message(role="assistant", content="a short summary")
        else:
            self.first_size = self.first_size or _size(messages)
            if _size(messages) > self.first_size + self.headroom:
                self.overflows += 1
                raise ProviderError(
                    "maximum context length is 1000 tokens, however you requested 9000", status_code=400
                )
            self.calls += 1
            if self.calls <= self.steps:
                path = str(self.root / f"f{self.calls}.txt")
                call = ToolCall(id=f"c{self.calls}", name="read", arguments={"path": path})
                reply = Message(role="assistant", content=f"step {self.calls}", tool_calls=[call])
            else:
                reply = Message(role="assistant", content="finished")
        yield StreamEvent(type="done", message=reply, usage=Usage())

    async def aclose(self) -> None:
        return None


async def long_turn_session(tmp_path, monkeypatch, provider, **settings):
    server, _ = make_server(tmp_path, monkeypatch, ["unused"], **settings)
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    server._tiers = None
    live = server.live[await new_session(server, tmp_path)]
    for i in range(1, provider.steps + 1):
        (tmp_path / f"f{i}.txt").write_text(f"file {i}\n" + "x" * 1500 + "\n")
    return server, live


# ── 1. overflow recovery folds the turn it is in ──────────────────────────


async def test_a_long_single_turn_in_a_fresh_session_is_folded_and_goes_on(tmp_path, monkeypatch):
    """One prompt and many tool calls: nothing older than the turn to fold, the turn used to die at the window."""
    provider = Climbing(tmp_path, steps=40, headroom=30_000)
    server, live = await long_turn_session(tmp_path, monkeypatch, provider)
    status, text = await server._run_turn(live, "read every file")
    assert status == "done", (text, live.last_error)
    assert provider.overflows >= 1 and provider.summaries == provider.overflows
    msgs = live.stored.messages
    assert any(str(m["content"]).startswith(SUMMARY_PREFIX) for m in msgs)
    assert [m["content"] for m in msgs if m["content"] == "read every file"] == ["read every file"]
    assert COMPACTED_CONTINUATION in [m["content"] for m in msgs if m["role"] == "user"]
    assert msgs[-1]["content"] == "finished"
    await server.close()


async def test_a_long_turn_after_a_short_history_costs_one_summary_per_overflow(tmp_path, monkeypatch):
    """The older history is tiny: a summary of it alone left the retry as long as it was, and the turn failed."""
    provider = Climbing(tmp_path, steps=40, headroom=30_000)
    server, live = await long_turn_session(tmp_path, monkeypatch, provider)
    live.stored.messages = long_history(4)
    status, text = await server._run_turn(live, "read every file, PROMPT-VERBATIM")
    assert status == "done", (text, live.last_error)
    assert provider.overflows >= 1 and provider.summaries == provider.overflows
    msgs = live.stored.messages
    summary_at = next(i for i, m in enumerate(msgs) if str(m["content"]).startswith(SUMMARY_PREFIX))
    # the turn's own prompt stays in view, right after the summary, whole
    assert msgs[summary_at + 1]["content"] == "read every file, PROMPT-VERBATIM"
    assert [m["content"] for m in msgs].count("read every file, PROMPT-VERBATIM") == 1
    await server.close()


async def test_a_turn_that_overflows_again_after_a_fold_is_folded_again(tmp_path, monkeypatch):
    provider = Climbing(tmp_path, steps=40, headroom=22_000)
    server, live = await long_turn_session(tmp_path, monkeypatch, provider)
    status, text = await server._run_turn(live, "read every file")
    assert status == "done", (text, live.last_error)
    assert provider.overflows >= 3 and provider.summaries == provider.overflows
    assert live.stored.meta["compactions"] == provider.summaries
    await server.close()


async def test_a_fold_needs_the_steps_the_last_retry_made(tmp_path, monkeypatch):
    """A retry that overflows at its first call (what is kept is over the limit by itself) ends the turn as before,
    instead of folding again and again."""
    provider = Climbing(tmp_path, steps=40, headroom=30_000)
    server, live = await long_turn_session(tmp_path, monkeypatch, provider)
    original = provider.stream

    async def reject_after_first_fold(messages, tools, model, **kw):
        if provider.summaries and not str(messages[0].content).startswith("Summarize"):
            provider.overflows += 1
            raise ProviderError("maximum context length is 1000 tokens, however you requested 9000", status_code=400)
        async for event in original(messages, tools, model, **kw):
            yield event

    provider.stream = reject_after_first_fold  # type: ignore[method-assign]
    status, _ = await server._run_turn(live, "read every file")
    assert status == "error" and isinstance(live.last_exc, ContextOverflow)
    assert provider.summaries == 1
    await server.close()


def _big_turn(small_turns: int = 8, steps: int = 60) -> list[dict]:
    msgs = long_history(small_turns)
    msgs.append({"role": "user", "content": "refactor everything"})
    for i in range(steps):
        call = {"id": f"c{i}", "name": "bash", "arguments": {"command": f"step {i}"}}
        msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
        msgs.append({"role": "tool", "name": "bash", "tool_call_id": f"c{i}", "content": f"out {i} " + "x" * 300})
    return msgs


async def test_the_cut_that_frees_the_most_is_taken_when_the_usual_one_frees_next_to_nothing():
    msgs = _big_turn()
    new, folded = await compact_messages(FakeCaller("s"), msgs, keep=8)
    assert folded > len(msgs) - 12, folded  # the cut at the last user message folded 17 of 258 messages
    assert new[-8:] == msgs[-8:]


async def test_the_usual_cut_is_kept_when_it_frees_about_as_much():
    msgs = long_history(30)
    _, folded = await compact_messages(FakeCaller("s"), msgs, keep=6)
    assert msgs[folded]["role"] == "user"  # a cut at a user message: the question stays with its answer


async def test_a_pinned_prompt_stays_after_the_summary_when_the_fold_reaches_past_it():
    msgs = _big_turn()
    pin = next(m for m in msgs if m["content"] == "refactor everything")
    new, folded = await compact_messages(FakeCaller("s"), msgs, keep=8, pin=pin)
    at = next(i for i, m in enumerate(new) if str(m["content"]).startswith(SUMMARY_PREFIX))
    assert new[at + 1] == pin
    # not when it is in the kept tail anyway, nor twice
    new, _ = await compact_messages(FakeCaller("s"), msgs, keep=len(msgs) - msgs.index(pin) + 1, pin=pin)
    assert [m["content"] for m in new].count("refactor everything") == 1


# ── 2. a prompt is clipped only when it alone cannot fit ──────────────────


class TooLong:
    """Rejects a request of more than ``limit`` characters as too long, else answers."""

    name = "fake"
    base_url = "fake://"

    def __init__(self, limit: int) -> None:
        self.limit = limit

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        if "Summarize this coding conversation" not in str(messages[0].content) and _size(messages) > self.limit:
            raise ProviderError("maximum context length is 1000 tokens, however you requested 9000", status_code=400)
        yield StreamEvent(type="done", message=Message(role="assistant", content="ok"), usage=Usage())

    async def aclose(self) -> None:
        return None


async def windowed_session(tmp_path, monkeypatch, provider, window: int, **settings):
    server, _ = make_server(tmp_path, monkeypatch, ["unused"], **settings)
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    server._tiers = None
    live = server.live[await new_session(server, tmp_path)]
    server.config.models = {server._active_model(live): {"context_window": window}}
    return server, live


def notices(server, key: str) -> list[dict]:
    return [
        e["payload"]
        for e in server.event_log
        if e["type"] == "notification.show" and str(e["payload"].get("key", "")).startswith(key)
    ]


async def test_a_prompt_that_fits_the_window_is_not_clipped_when_the_history_caused_the_overflow(tmp_path, monkeypatch):
    server, live = await windowed_session(
        tmp_path, monkeypatch, TooLong(60_000), 32_000, context={"autocompact": False}
    )
    live.stored.messages = long_history(40)  # ~35k characters
    prompt = "p" * 40_000  # ~10k tokens: fits a 32k window on its own
    status, _ = await server._run_turn(live, prompt)
    assert status == "error" and isinstance(live.last_exc, ContextOverflow)
    assert live.stored.messages[-1]["content"] == prompt
    assert not notices(server, "k3.prompt_clipped")
    await server.close()


async def test_a_prompt_larger_than_the_window_is_clipped_and_the_user_is_told(tmp_path, monkeypatch):
    server, live = await windowed_session(tmp_path, monkeypatch, TooLong(60_000), 8_000)
    prompt = "q" * 100_000  # ~25k tokens: more than an 8k window can take
    status, _ = await server._run_turn(live, prompt)
    assert status == "error" and isinstance(live.last_exc, ContextOverflow)
    kept = live.stored.messages[-1]["content"]
    assert kept.startswith("q" * 1000) and "removed: this message alone is larger than the context window" in kept
    removed = len(prompt) - kept.index("\n…[")
    [note] = notices(server, "k3.prompt_clipped")
    assert note["level"] == "warning" and note["kind"] == "ttl" and f"{removed:,} of its 100,000" in note["text"]
    assert len(kept) < 8_000 * 4  # what stays is a share of the window
    await server.close()


# ── 3. the summary call is sized to the summarizer's window ───────────────


def _chat(n: int, width: int) -> list[dict]:
    out = [{"role": "system", "content": "sys"}]
    for i in range(n):
        out.append({"role": "user", "content": f"request {i} " + "x" * width})
        out.append({"role": "assistant", "content": f"answer {i} " + "y" * width})
    return out


async def test_the_summary_input_is_capped_to_the_summary_models_tokens():
    from k3code.context_budget import text_tokens

    caller = FakeCaller("s")
    await compact_messages(caller, _chat(200, 600), keep=6, max_input_tokens=3_000)
    assert text_tokens(caller.calls[0][1].content) <= 3_000
    plain = FakeCaller("s")
    await compact_messages(plain, _chat(200, 600), keep=6)
    assert len(plain.calls[0][1].content) > 4 * 3_000  # unchanged without a cap


async def test_the_summary_is_asked_again_with_less_when_the_model_says_it_is_too_long():
    class Overflowing(FakeCaller):
        async def complete(self, kind, messages, **kw):
            if not self.calls:
                self.calls.append(messages)
                raise ContextOverflow("too long")
            return await super().complete(kind, messages, **kw)

    caller = Overflowing("the summary")
    new, folded = await compact_messages(caller, _chat(200, 600), keep=6)
    assert folded and any(m["content"] == SUMMARY_PREFIX + "the summary" for m in new)
    assert len(caller.calls) == 2 and len(caller.calls[1][1].content) <= len(caller.calls[0][1].content) // 4 + 100


async def test_a_second_overflow_of_the_summary_call_fails_the_compaction():
    class AlwaysTooLong(FakeCaller):
        async def complete(self, kind, messages, **kw):
            self.calls.append(messages)
            raise ContextOverflow("still too long")

    caller = AlwaysTooLong()
    with pytest.raises(ContextOverflow):
        await compact_messages(caller, _chat(200, 600), keep=6)
    assert len(caller.calls) == 2


async def test_the_summary_window_is_the_compaction_tiers_not_the_main_tiers(tmp_path, monkeypatch):
    server, live = await windowed_session(tmp_path, monkeypatch, TooLong(10**9), 200_000)
    provider_cfg = server.config.providers[0]
    provider_cfg.models = {"default": "big-model", "cheap": "small-model"}
    server.config.models = {"big-model": {"context_window": 200_000}, "small-model": {"context_window": 8_000}}
    assert server._summary_window(live) == 8_000
    assert server._compaction_model(live) == "big-model"
    await server.close()


async def test_a_failed_automatic_compaction_tells_the_user(tmp_path, monkeypatch):
    server, live = await windowed_session(tmp_path, monkeypatch, TooLong(10**9), 8_000)
    live.stored.messages = long_history(40)

    async def failing(*args, **kw):
        raise RuntimeError("the summary model is down")

    server.model_caller.complete = failing  # type: ignore[method-assign]
    assert await server._maybe_compact(live) == 0
    [note] = notices(server, "k3.compact_failed")
    assert note["level"] == "warning" and note["kind"] == "ttl" and note["ttl_ms"] == 15000
    assert "the summary model is down" in note["text"]
    assert live.compact_retry_at > 0
    await server.close()


# ── 4. the compaction trigger is calibrated against what the provider reports ──


class Reporting:
    """Answers every call and reports ``prompt_tokens`` for it (the estimate says a good deal less)."""

    name = "fake"
    base_url = "fake://"

    def __init__(self, prompt_tokens: int, cost_usd: float | None = None) -> None:
        self.prompt_tokens, self.cost_usd = prompt_tokens, cost_usd
        self.summaries = 0

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        reply = "ok"
        if "Summarize this coding conversation" in str(messages[0].content):
            self.summaries += 1
            reply = "a short summary"
        usage = Usage(prompt_tokens=self.prompt_tokens, completion_tokens=5, cost_usd=self.cost_usd)
        message = Message(role="assistant", content=reply, usage=usage)
        yield StreamEvent(type="done", message=message, usage=usage)

    async def aclose(self) -> None:
        return None


async def calibrated_session(tmp_path, monkeypatch, provider, **settings):
    server, live = await windowed_session(tmp_path, monkeypatch, provider, 100_000, **settings)
    live.stored.messages = long_history(50)  # ~40,000 characters: ~10,000 estimated tokens
    return server, live


async def test_the_trigger_follows_what_the_provider_counted_not_the_estimate(tmp_path, monkeypatch):
    provider = Reporting(30_000)
    server, live = await calibrated_session(tmp_path, monkeypatch, provider, context={"compact_at_tokens": 20_000})
    estimate = server._request_tokens(live)
    assert estimate < 20_000
    assert (await server._run_turn(live, "first"))[0] == "done"
    assert provider.summaries == 0  # the estimate was under the limit when this turn began
    assert 2.0 < live.token_ratio <= 3.0 and not live.token_ratio_stale
    assert server._request_tokens(live) > 25_000  # about what the provider said
    assert live.stored.meta["token_ratio"]["ratio"] == round(live.token_ratio, 3)
    assert (await server._run_turn(live, "second"))[0] == "done"
    assert provider.summaries == 1  # and now the limit is reached
    await server.close()


async def test_the_status_bar_and_the_trigger_use_the_same_number(tmp_path, monkeypatch):
    server, live = await calibrated_session(tmp_path, monkeypatch, Reporting(30_000))
    await server._run_turn(live, "first")
    assert server.context_fields(live)["context_used"] == server._request_tokens(live) > 20_000
    await server.close()


async def test_the_ratio_is_bounded_and_starts_again_on_a_model_switch(tmp_path, monkeypatch):
    server, live = await calibrated_session(tmp_path, monkeypatch, Reporting(900_000))
    await server._run_turn(live, "first")
    assert live.token_ratio == 3.0
    live.stored.model = "another-model"
    assert server._token_ratio(live) == 1.0 and live.token_ratio_stale
    await server.close()


async def test_a_provider_that_reports_a_cost_is_not_calibrated(tmp_path, monkeypatch):
    """claude-cli counts the Claude Code prompt and tools as well: the ratio would take that for the conversation."""
    server, live = await calibrated_session(tmp_path, monkeypatch, Reporting(30_000, cost_usd=0.01))
    await server._run_turn(live, "first")
    assert live.token_ratio == 1.0 and live.token_ratio_stale
    await server.close()


async def test_a_compaction_makes_the_ratio_stale_and_the_next_sample_replaces_it(tmp_path, monkeypatch):
    server, live = await calibrated_session(tmp_path, monkeypatch, Reporting(30_000))
    await server._run_turn(live, "first")
    assert not live.token_ratio_stale
    assert await server.compact_session(live)
    assert live.token_ratio_stale and live.token_samples == 0
    await server.close()


# ── 5. a resumed or forked session keeps what is elided; the prompt snapshot too ──


async def elided_session(tmp_path, monkeypatch):
    """A session that has run a long turn on a small window: a good part of its tool results are elided."""
    provider = Climbing(tmp_path, steps=30, headroom=10**9)
    server, live = await long_turn_session(tmp_path, monkeypatch, provider)
    model = server._active_model(live)  # the window is looked up by the model the router sends to
    server.router = Router(build_chain([provider], [[model]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    server.config.models = {model: {"context_window": 12_000}}
    status, text = await server._run_turn(live, "read every file")
    assert status == "done", (text, live.last_error)
    assert len(live.elided) >= 5
    return server, live


def reopened(server, live):
    """The session as a daemon that was restarted (or a resume after a close) builds it from the store."""
    from k3code.gateway.server import LiveSession

    return LiveSession(live.session_id, server.store.get(live.session_id), server)


async def test_a_resumed_session_estimates_its_request_as_the_loop_sends_it(tmp_path, monkeypatch):
    server, live = await elided_session(tmp_path, monkeypatch)
    again = reopened(server, live)
    assert again.elided == live.elided
    # the conversation (what the system prompt and tools weigh is measured by each session's own first loop)
    conversation = server._request_tokens(again) - again.overhead_tokens
    assert conversation == server._request_tokens(live) - live.overhead_tokens
    # all of the results at full size is a good deal more: the first prompt after the resume used to compact
    live.elided.clear()
    assert server._request_tokens(live) - live.overhead_tokens > 1.5 * conversation
    await server.close()


async def test_a_fork_starts_with_the_elision_of_its_source(tmp_path, monkeypatch):
    from k3code.commands.fork import fork_session
    from k3code.gateway.server import LiveSession

    server, live = await elided_session(tmp_path, monkeypatch)
    child = fork_session(server.store, server.store.get(live.session_id))
    assert LiveSession(child.session_id, child, server).elided == live.elided
    await server.close()


async def test_the_elision_in_the_meta_is_bounded_and_follows_the_history(tmp_path, monkeypatch):
    from k3code.gateway.server import ELIDED_META_IDS, _elision_meta

    server, live = await elided_session(tmp_path, monkeypatch)
    meta = server.store.get(live.session_id).meta
    assert set(meta["elided"]) == live.elided
    ids = [f"c{i}" for i in range(ELIDED_META_IDS + 50)]
    messages = [{"role": "tool", "tool_call_id": i, "content": "x"} for i in ids]
    kept = _elision_meta(messages, set(ids) | {"gone"}, {ids[-1]: "a note", "gone": "stale"})
    assert kept["elided"] == ids[-ELIDED_META_IDS:] and kept["elide_notes"] == {ids[-1]: "a note"}
    assert _elision_meta([], {"c1"}, {"c1": "n"}) == {}  # nothing of it is in the history: nothing is kept
    # a compaction empties it, in memory and in the meta
    assert await server.compact_session(live)
    assert not live.elided and "elided" not in live.stored.meta
    await server.close()


async def test_the_result_a_read_pointer_names_is_pinned_again_after_a_resume(tmp_path, monkeypatch):
    from k3code.gateway.server import LiveSession

    server, _ = make_server(tmp_path, monkeypatch, ["unused"])
    stored = server.store.create(title="t", model="m", cwd=str(tmp_path))
    pointer = "[unchanged since the read at step 1 (call r1): same path, size and modification time; that holds]"
    stored.messages = [
        {"role": "user", "content": "read it twice"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "r1", "name": "read", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "r1", "name": "read", "content": "x" * 2000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "r2", "name": "read", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "r2", "name": "read", "content": pointer},
    ]
    stored.meta["elided"] = ["r1", "r2", "no-such-result"]
    live = LiveSession(stored.session_id, stored, server)
    assert live.pinned_reads == {"r1"}
    assert live.elided == {"r1", "r2"}  # an id the history does not have is dropped
    await server.close()


# ── (b) the prompt snapshot ──


async def test_a_resumed_session_keeps_the_prompt_it_was_built_with(tmp_path, monkeypatch):
    from k3code.memory import write_learned

    server, live = await calibrated_session(tmp_path, monkeypatch, Reporting(1000))
    await server._run_turn(live, "first")
    assert live.learned_notes is not None and live.prompt_frozen
    seen = (list(live.learned_notes), dict(live.prompt_frozen))
    write_learned(tmp_path, ["a note a background review wrote meanwhile"])  # what a re-read would now show
    again = reopened(server, live)
    assert (again.learned_notes, again.prompt_frozen) == seen
    await server.close()


async def test_an_old_prompt_snapshot_is_not_kept_and_a_refresh_forgets_it(tmp_path, monkeypatch):
    from k3code.gateway.server import SNAPSHOT_MAX_AGE_S

    server, live = await calibrated_session(tmp_path, monkeypatch, Reporting(1000))
    await server._run_turn(live, "first")
    saved = server.store.get(live.session_id)
    assert saved.meta["prompt_snapshot"]["frozen"] == {
        k: v for k, v in saved.meta["prompt_snapshot"]["frozen"].items() if isinstance(v, str | list)
    }
    saved.meta["prompt_snapshot"]["at"] -= SNAPSHOT_MAX_AGE_S + 1  # the provider's cache has expired by now
    server.store.save(saved)
    assert reopened(server, live).prompt_frozen == {} and reopened(server, live).learned_notes is None
    live.refresh_prompt_inputs()
    assert "prompt_snapshot" not in live.stored.meta
    await server.close()


# ── 6. the summary ends with a ledger of the files and commands of the folded calls ──


def _steps(start: int, count: int) -> list[dict]:
    out: list[dict] = []
    for i in range(start, start + count):
        for name, args in (
            ("write", {"path": f"/work/src/module_{i:03d}.py", "content": "x"}),
            ("bash", {"command": f"pytest -q tests/test_{i}.py\nsecond line"}),
            ("read", {"path": f"/work/docs/page_{i:03d}.md"}),
        ):
            out.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": f"{name}{i}", "name": name, "arguments": args}],
                }
            )
            out.append({"role": "tool", "tool_call_id": f"{name}{i}", "name": name, "content": "ok"})
    return out


def _compactable(steps: list[dict]) -> list[dict]:
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "TASK: migrate"}, *steps]


async def test_the_summary_carries_a_ledger_of_the_files_and_commands_it_folded():
    from k3code.session_ai import LEDGER_HEADER

    msgs = _compactable(_steps(0, 12))
    new, folded = await compact_messages(FakeCaller("the model's summary"), msgs, keep=6)
    summary = next(m["content"] for m in new if m["content"].startswith(SUMMARY_PREFIX))
    assert summary.startswith(SUMMARY_PREFIX + "the model's summary\n\n" + LEDGER_HEADER)
    assert "changed: /work/src/module_000.py" in summary and "ran: pytest -q tests/test_8.py…" in summary
    assert "read: /work/docs/page_008.md" in summary and "page_001" not in summary  # the newest of each kind
    # only what was folded: the kept tail's calls are in the tail
    kept_paths = {
        c["arguments"]["path"] for m in new[3:] for c in m.get("tool_calls") or [] if "path" in c["arguments"]
    }
    assert kept_paths and not any(f"changed: {p}" in summary for p in kept_paths)
    assert len(summary.split(LEDGER_HEADER)[1].strip().splitlines()) <= 40


async def test_the_ledger_is_merged_across_compactions_and_capped_to_the_newest():
    from k3code.session_ai import LEDGER_HEADER, LEDGER_MAX

    msgs = _compactable(_steps(0, 40))
    first, _ = await compact_messages(FakeCaller("one"), msgs, keep=6)
    # the summary model restates the ledger it was shown, in its own words and layout
    restating = FakeCaller(f"two\n{LEDGER_HEADER}\nchanged: /made/up/path.py\nread: /also/made/up.md")
    second, _ = await compact_messages(restating, [*first, *_steps(40, 40)], keep=6)
    summary = next(m["content"] for m in second if m["content"].startswith(SUMMARY_PREFIX))
    assert summary.count(LEDGER_HEADER) == 1 and "/made/up" not in summary  # one ledger, built from the calls
    lines = [ln for ln in summary.splitlines() if ln.startswith("changed: ")]
    assert len(lines) == LEDGER_MAX["changed"] and len(set(lines)) == len(lines)
    assert lines[-1].startswith("changed: /work/src/module_07") and "(older entries not listed)" in summary
    # earlier entries survive while they are among the newest: those of the first summary, whose calls are gone
    early, _ = await compact_messages(FakeCaller("one"), _compactable(_steps(0, 6)), keep=6)
    later, _ = await compact_messages(FakeCaller("two"), [*early, *_steps(6, 8)], keep=6)
    summary2 = next(m["content"] for m in later if m["content"].startswith(SUMMARY_PREFIX))
    assert "changed: /work/src/module_000.py" in summary2 and "changed: /work/src/module_009.py" in summary2
    assert summary2.count("changed: /work/src/module_000.py") == 1


async def test_a_conversation_without_tool_calls_gets_no_ledger():
    msgs = long_history(30)
    new, _ = await compact_messages(FakeCaller("s"), msgs, keep=6)
    summary = next(m["content"] for m in new if m["content"].startswith(SUMMARY_PREFIX))
    assert summary == SUMMARY_PREFIX + "s"


# ── 11. a todo list restated by the summary model is replaced, not doubled ──


async def test_the_todo_list_is_not_doubled_by_a_second_compaction(tmp_path, monkeypatch):
    from k3code.tools import render_todos

    todos = [{"content": "step A", "status": "completed"}, {"content": "step B", "status": "in_progress"}]
    server, live = await windowed_session(tmp_path, monkeypatch, TooLong(10**9), 100_000, context={"keep_messages": 6})
    live.stored.meta["todos"] = todos
    old_list = render_todos([{"content": "step A", "status": "in_progress"}])
    summary = {"role": "user", "content": f"{SUMMARY_PREFIX}Summary.\n\n{old_list}"}
    folded = [summary]
    new = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": f"{SUMMARY_PREFIX}Restated. {old_list}\nafter"},
    ]
    [_, out] = server._with_todos(live, folded, new)
    assert out["content"].count("Todos (") == 1 and render_todos(todos) in out["content"]
    assert "[>] step A" not in out["content"] and "Restated." in out["content"] and "after" in out["content"]
    await server.close()


# ── 7. the task statement kept verbatim is sized to a small window ──


async def test_the_kept_task_statement_takes_a_tenth_of_a_small_window():
    from k3code.session_ai import MIN_ANCHOR_CHARS, TASK_ANCHOR_CHARS, anchor_chars

    paste = "TASK: " + "p" * 14_000 + " Constraint: never touch /legacy"
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": paste}, *long_history(30)[1:]]
    kept = {}
    for window in (None, 8_000, 200_000, 2_000):
        new, _ = await compact_messages(FakeCaller("s"), msgs, keep=6, window=window)
        kept[window] = next(m["content"] for m in new if m["content"].startswith("TASK: "))
    assert len(kept[None]) <= TASK_ANCHOR_CHARS and len(kept[None]) > 11_000  # unchanged without a window
    assert len(kept[200_000]) == len(kept[None])  # a tenth of 200k tokens is more than the cap
    assert len(kept[8_000]) <= anchor_chars(8_000, paste) + 60 < len(kept[None]) // 2
    assert kept[8_000].endswith("Constraint: never touch /legacy")  # the requirement at its end stays
    assert anchor_chars(2_000, paste) == MIN_ANCHOR_CHARS and len(kept[2_000]) <= MIN_ANCHOR_CHARS + 60  # a floor
    assert anchor_chars(8_000, "今天我们讨论" * 500) < anchor_chars(8_000, "x" * 3_000)  # fewer characters a token


async def test_the_gateway_sizes_the_kept_task_statement_to_the_smallest_window(tmp_path, monkeypatch):
    from k3code.session_ai import anchor_chars

    server, live = await windowed_session(tmp_path, monkeypatch, Reporting(1000), 8_000)
    task = "TASK: " + "p" * 14_000
    live.stored.messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": task},
        *long_history(30)[1:],
    ]
    assert await server.compact_session(live)
    anchor = next(m["content"] for m in live.stored.messages if m["content"].startswith("TASK: "))
    assert len(anchor) <= anchor_chars(8_000, task) + 60 < 6_000
    await server.close()


# ── 8. an overflow before any tool ran does not plan the task again ──


async def test_an_overflow_before_any_tool_ran_reuses_the_scope_gates_plan(tmp_path, monkeypatch):
    from k3code.autonomy.plan_first import GateResult
    from test_auto_compaction import Recording, seed

    users: list[str] = []

    class Seeing(Recording):
        async def stream(self, messages, tools, model, **kw):
            users.append(str(messages[-1].content))
            async for event in super().stream(messages, tools, model, **kw):
                yield event

    server, live = await seed(
        tmp_path, monkeypatch, Seeing(overflow_over=6000), context={"compact_at_tokens": 10_000_000, "keep_messages": 6}
    )
    asked: list[str] = []

    async def prepare(session, text):  # the classifier, the planning turn and the user's approval
        asked.append(text)
        return GateResult(prompt=f"{text}\n\nApproved plan (follow it):\nPLAN-ONE", plan="PLAN-ONE")

    server.autonomy.prepare = prepare  # type: ignore[method-assign]
    status, text = await server._run_turn(live, "NEWPROMPT please")
    assert status == "done", (text, live.last_error)
    assert asked == ["NEWPROMPT please"]  # once, not again for the rerun
    sent = [u for u in users if "NEWPROMPT" in u]
    assert len(sent) == 2 and all("PLAN-ONE" in u for u in sent)  # and the rerun still carries the approved plan
    await server.close()


# ── 9. a trivial prompt starts on the cheap tier only when the conversation fits its window ──


class Named(Reporting):
    """Answers with its own name, so a test sees which tier took the turn."""

    def __init__(self, label: str) -> None:
        super().__init__(100)
        self.label, self.turns = label, 0

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        if "Summarize this coding conversation" not in str(messages[0].content):
            self.turns += 1
        reply = Message(role="assistant", content=self.label)
        yield StreamEvent(type="done", message=reply, usage=Usage())


async def two_tier_session(tmp_path, monkeypatch, history_turns: int):
    from k3code.routing.tiers import Tier

    class Tiers:
        def __init__(self, routers):
            self.routers = routers

        def get(self, tier):
            return self.routers[Tier(tier)]

        def for_kind(self, kind):
            return self.routers[Tier.CHEAP]

    main, cheap = Named("main"), Named("cheap")
    server, live = await windowed_session(tmp_path, monkeypatch, main, 200_000, permission_mode="yolo")
    server.config.providers[0].models = {"default": "big-model", "cheap": "small-model"}
    server.config.models = {"big-model": {"context_window": 200_000}, "small-model": {"context_window": 16_000}}
    server.router = Router(build_chain([main], [["big-model"]]), max_retries=0)
    small = Router(build_chain([cheap], [["small-model"]]), max_retries=0)
    server._tiers = Tiers({t: (small if t in (Tier.CHEAP, Tier.FAST) else server.router) for t in Tier})
    live.stored.messages = long_history(history_turns)
    live.scope_override = "trivial"
    return server, live, main, cheap


async def test_a_trivial_prompt_starts_on_the_cheap_tier_when_the_conversation_fits_it(tmp_path, monkeypatch):
    server, live, main, cheap = await two_tier_session(tmp_path, monkeypatch, 3)
    assert (await server._run_turn(live, "fix the typo in README.md"))[0] == "done"
    assert (cheap.turns, main.turns) == (1, 0)
    await server.close()


async def test_a_trivial_prompt_stays_on_the_main_tier_when_the_conversation_does_not_fit_the_cheap_one(
    tmp_path, monkeypatch
):
    server, live, main, cheap = await two_tier_session(tmp_path, monkeypatch, 80)  # ~16k tokens: over 70% of 16k
    assert server._request_tokens(live) < 100_000  # nowhere near compaction on the main tier
    assert (await server._run_turn(live, "fix the typo in README.md"))[0] == "done"
    assert (cheap.turns, main.turns) == (0, 1)
    await server.close()


# ── 10. /autocompact says what the trigger does ──


async def test_autocompact_reports_the_window_the_trigger_uses(tmp_path, monkeypatch):
    from m1cmd_helpers import cmd

    server, live = await windowed_session(tmp_path, monkeypatch, Reporting(100), 200_000)
    server.config.providers[0].models = {
        "default": ["big-model", "small-model"]
    }  # a failover chain: the smaller counts
    server.config.models = {"big-model": {"context_window": 200_000}, "small-model": {"context_window": 16_000}}
    assert server._active_model(live) == "big-model" and server._compaction_model(live) == "small-model"
    out = await cmd(server, "/autocompact", live.session_id)
    assert "11,200 of 16,000 tokens for small-model" in (out.get("output") or out.get("message") or str(out))
    assert out["autocompact"]["window"] == 16_000 and out["autocompact"]["threshold"] == 11_200
    assert server.context_fields(live)["autocompact_at"] == 11_200  # the status bar and the command agree
    await server.close()


# ── 1 (again). the fold after an overflow goes on while the retry would still take over half the window ──


async def test_a_second_fold_follows_when_the_first_left_the_long_turn_whole(tmp_path, monkeypatch):
    from types import SimpleNamespace

    server, live = await windowed_session(tmp_path, monkeypatch, Reporting(100), 200_000)
    older = long_history(40)
    turn = [{"role": "user", "content": "PROMPT-VERBATIM"}]
    for i in range(24):
        call = {"id": f"c{i}", "name": "bash", "arguments": {"command": f"step {i}"}}
        turn += [
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "name": "bash", "tool_call_id": f"c{i}", "content": f"out {i} " + "x" * 1200},
        ]
    # the older history weighs about what the turn does: a cut at the turn's prompt frees the most but leaves the turn
    half_of_the_turn = server._request_tokens(live, turn) // 2
    assert 0.8 < server._request_tokens(live, older) / server._request_tokens(live, turn) < 1.3
    server.config.models = {server._active_model(live): {"context_window": 2 * half_of_the_turn}}
    asked: list[str] = []

    async def complete(kind, messages, **kw):
        asked.append(str(messages[1].content))
        return SimpleNamespace(text="a summary")

    server.model_caller.complete = complete  # type: ignore[method-assign]
    failed = [*older, *turn]
    live.stored.messages = failed
    assert await server._fold_after_overflow(live, failed, len(older), turn[0])
    assert len(asked) == 2  # the older history, then the turn's earlier steps
    msgs = live.stored.messages
    assert server._request_tokens(live) <= half_of_the_turn
    summary_at = next(i for i, m in enumerate(msgs) if str(m["content"]).startswith(SUMMARY_PREFIX))
    assert msgs[summary_at + 1] == turn[0] and msgs[-1] == turn[-1]  # the prompt after the summary, the last step whole
    await server.close()


async def test_a_clipped_task_statement_is_left_alone_by_the_next_compactions():
    pastes = {
        "prose": "TASK: " + "p" * 14_000 + " Constraint: never touch /legacy",
        "code": "TASK: migrate the module. Use `FooBarBaz.handleRequest(ctx, 42)` and SNAKE_CASE_0x1F. " * 160,
        "dense in the middle": "TASK: fix it. "
        + "a1b2c3d4e5f6" * 300
        + "the quick brown fox. " * 300
        + " never touch /legacy",
        "cjk": "任务：" + "今天我们讨论一下这个项目的整体架构设计。" * 400 + "约束：不要修改 /legacy",
    }
    for kind, paste in pastes.items():
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": paste}, *long_history(30)[1:]]
        anchors = []
        for _ in range(4):
            new, folded = await compact_messages(FakeCaller("s"), msgs, keep=6, window=8_000)
            assert folded
            anchors.append(new[1]["content"])
            msgs = [*new, *long_history(30)[1:]]
        assert anchors[0] != paste and anchors[0].count("chars omitted") == 1, kind
        assert anchors[1:] == [anchors[0]] * 3, kind  # byte for byte: one marker, the original count of omitted chars


async def test_a_call_made_before_the_turns_loop_ran_is_not_a_sample(tmp_path, monkeypatch):
    """The planning turn reports its calls through the same place, while the turn's own loop has no messages yet."""
    from types import SimpleNamespace

    server, live = await calibrated_session(tmp_path, monkeypatch, Reporting(30_000))
    live.loop = SimpleNamespace(turn_messages=[])
    server._calibrate(live, Usage(prompt_tokens=30_000))
    assert live.token_samples == 0 and live.token_ratio_stale
    await server.close()


# ── the estimate counts a result as the loop sends it: cut to the window, cut down further to fit ──


def test_a_result_is_estimated_at_its_share_of_the_window():
    from k3code.context_budget import SHRINK_FLOOR_CHARS, SHRINK_SHARE, result_chars, text_tokens
    from k3code.gateway.server import _estimate_tokens
    from k3code.tools import clip_for_model, shrink_result

    big = "".join(f"line {i:05d}: the committee reviewed the quarterly results for item {i}.\n" for i in range(1000))
    result = [{"role": "tool", "name": "read", "tool_call_id": "r1", "content": big}]
    whole = _estimate_tokens(result)
    clipped = _estimate_tokens(result, None, (), 8_000)
    assert clipped == text_tokens(clip_for_model("read", big, window=8_000)) < whole // 2
    # a result the loop cut down further (clip:<id>) is counted as that cut
    cut = result_chars(clip_for_model("read", big, window=8_000), 8_000, SHRINK_SHARE, SHRINK_FLOOR_CHARS)
    shrunk = shrink_result("read", clip_for_model("read", big, window=8_000), cut)
    assert _estimate_tokens(result, None, {"clip:r1"}, 8_000) == text_tokens(shrunk) < clipped


async def test_the_request_after_a_big_read_is_estimated_within_a_small_window(tmp_path, monkeypatch):
    big = tmp_path / "big.txt"
    big.write_text(
        "".join(f"line {i:05d}: the committee reviewed the quarterly results for item {i}.\n" for i in range(1000))
    )

    class ReadsOnce(Reporting):
        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            if "Summarize this coding conversation" in str(messages[0].content) or any(
                m.role == "tool" for m in messages
            ):
                reply = Message(role="assistant", content="done")
            else:
                call = ToolCall(id="r1", name="read", arguments={"path": str(big)})
                reply = Message(role="assistant", content="", tool_calls=[call])
            yield StreamEvent(type="done", message=reply, usage=Usage())

    server, live = await windowed_session(tmp_path, monkeypatch, ReadsOnce(1), 8_000)
    model = server._active_model(live)
    server.router = Router(build_chain([ReadsOnce(1)], [[model]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]
    assert (await server._run_turn(live, "read big.txt please"))[0] == "done"
    # 70 KB of text is more than the window; the loop sends a share of it, and so does the estimate
    assert server._request_tokens(live) < 8_000
    await server.close()


async def test_the_clip_marks_of_the_loop_are_kept_in_the_meta_and_restored(tmp_path, monkeypatch):
    from k3code.gateway.server import LiveSession, _elision_meta

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "r1", "name": "read", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "r1", "name": "read", "content": "x"},
    ]
    assert _elision_meta(messages, {"clip:r1", "r1", "clip:gone"}, {})["elided"] == ["r1", "clip:r1"]
    server, _ = make_server(tmp_path, monkeypatch, ["unused"])
    stored = server.store.create(title="t", model="m", cwd=str(tmp_path))
    stored.messages = messages
    stored.meta["elided"] = ["clip:r1", "clip:gone"]
    assert LiveSession(stored.session_id, stored, server).elided == {"clip:r1"}
    await server.close()
