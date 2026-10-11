"""Fixes from the second audit: provider parsing and classification, crash safety, what the clients are told."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from k3code.agent.loop import AgentLoop
from k3code.autonomy.plan_first import PlanFirst
from k3code.gateway.server import INTERRUPTED_RESULT, LiveSession
from k3code.providers.base import ProviderError
from k3code.providers.openai_compat import OpenAICompatProvider, _inband_error
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage, messages_to_openai
from k3code.reliability import Reliability
from k3code.reliability.journal import ToolJournal
from k3code.router import Router, build_chain
from k3code.router.classifier import FailoverReason, classify_api_error
from k3code.router.cooldown import _provider_reset_delay
from m1cmd_helpers import cmd, make_server, new_session, rpc
from test_auto_compaction import Recording, seed

# ── the stream of an OpenAI-compatible provider ───────────────────────


def _sse(*chunks: object) -> bytes:
    lines = [f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks]
    return "".join(lines).encode()


async def _stream(*chunks: object):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(*chunks), headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider(
        name="o",
        base_url="https://r.test/v1",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return [e async for e in p.stream([Message(role="user", content="go")], [], "gpt-test")]


def _delta(**tool_call: object) -> dict:
    return {"choices": [{"delta": {"tool_calls": [tool_call]}}]}


async def test_parallel_calls_numbered_zero_by_the_server_stay_separate_calls():
    events = await _stream(
        _delta(index=0, id="c1", function={"name": "read", "arguments": '{"path": "a"}'}),
        _delta(index=0, id="c2", function={"name": "read", "arguments": '{"path": "b"}'}),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )
    calls = events[-1].message.tool_calls
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("c1", "read", {"path": "a"}),
        ("c2", "read", {"path": "b"}),
    ]


async def test_a_name_repeated_in_every_delta_or_streamed_in_pieces_is_one_name():
    repeated = await _stream(
        _delta(index=0, id="c1", function={"name": "read", "arguments": '{"pa'}),
        _delta(index=0, function={"name": "read", "arguments": 'th": "a"}'}),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )
    assert [(c.name, c.arguments) for c in repeated[-1].message.tool_calls] == [("read", {"path": "a"})]
    pieces = await _stream(
        _delta(index=0, id="c1", function={"name": "re"}),
        _delta(index=0, function={"name": "ad", "arguments": "{}"}),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )
    assert [c.name for c in pieces[-1].message.tool_calls] == ["read"]


async def test_a_call_without_an_id_gets_a_fresh_one_every_time():
    """`call_0` repeated across turns made the journal match a new call to an old, finished one."""
    one = await _stream(_delta(index=0, function={"name": "bash", "arguments": "{}"}), "[DONE]")
    two = await _stream(_delta(index=0, function={"name": "bash", "arguments": "{}"}), "[DONE]")
    assert one[-1].message.tool_calls[0].id != two[-1].message.tool_calls[0].id


async def test_a_chunk_that_is_no_object_is_skipped():
    events = await _stream("[1]", {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}, "[DONE]")
    assert events[-1].message.content == "hi"


async def test_a_redirect_is_reported_as_one_instead_of_retried_as_a_dropped_connection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://r.test/v1/chat/completions"})

    p = OpenAICompatProvider(
        name="o",
        base_url="http://r.test/v1",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError) as err:
        [e async for e in p.stream([Message(role="user", content="go")], [], "gpt-test")]
    assert err.value.status_code == 301 and "redirects to https://r.test" in err.value.message
    assert classify_api_error(err.value, provider="o", model="m").reason is not FailoverReason.server


# ── classification and retry windows ──────────────────────────────────


def _classify(message: str, status: int = 400, **body: object) -> FailoverReason:
    err = ProviderError(message=message, status_code=status, headers={}, body={"error": {"message": message, **body}})
    return classify_api_error(err, provider="p", model="m").reason


def test_anthropics_input_plus_max_tokens_over_the_window_is_an_overflow_not_a_bad_request():
    msg = (
        "input length and `max_tokens` exceed context limit: 188240 + 20000 > 200000, "
        "decrease input length or `max_tokens`"
    )
    assert _classify(msg) is FailoverReason.context_overflow  # it failed over through every entry, never compacting
    assert _classify("max_tokens must be at most 8192") is FailoverReason.bad_request  # a rejected parameter stays one


@pytest.mark.parametrize(
    ("chunk", "reason"),
    [
        (
            {
                "error": {
                    "message": "You exceeded your quota",
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                }
            },
            FailoverReason.quota,
        ),
        ({"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}, FailoverReason.auth),
        ({"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}}, FailoverReason.rate_limit),
        ({"error": {"message": "upstream exploded"}}, FailoverReason.server),
        ({"error": {"message": "Overloaded", "code": 529}}, FailoverReason.server),
    ],
)
def test_an_error_in_the_stream_is_classified_by_its_name(chunk, reason):
    assert classify_api_error(_inband_error(chunk), provider="p", model="m").reason is reason


def test_a_provider_declared_wait_is_capped_and_a_zero_wait_is_not_a_hammer():
    assert _provider_reset_delay(86_400_000) == 7 * 86400.0  # `Retry-After: 86400000` armed a cooldown of ~3 years
    assert _provider_reset_delay(120) == 120
    router = Router(build_chain([SimpleNamespace(name="p", base_url="u")], [["m"]]), max_retries=0)
    zero = SimpleNamespace(retry_after=0.0)
    assert router._backoff_for(zero, 1) >= 0.5


# ── the loop ──────────────────────────────────────────────────────────


class _Silent:
    name = "fake"
    base_url = "fake://"

    def __init__(self, stop_reason: str) -> None:
        self.stop_reason = stop_reason

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        msg = Message(role="assistant", content=None, tool_calls=[], usage=Usage(), stop_reason=self.stop_reason)
        yield StreamEvent(type="done", message=msg, usage=Usage())

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize("reason", ["content_filter", "refusal"])
async def test_an_answer_the_provider_ended_without_a_word_says_so(tmp_path, reason):
    loop = AgentLoop(
        Router(build_chain([_Silent(reason)], [["m"]]), max_retries=0),
        system_prompt="s",
        permission_mode="yolo",
        cwd=tmp_path,
        session="s",
        reliability=Reliability.from_settings(None, session="s", home=tmp_path / "home"),
    )
    shown: list[str] = []

    async def on_delta(text: str) -> None:
        shown.append(text)

    loop.on_text_delta = on_delta
    async for _ in loop.run("hello"):
        pass
    last = loop.turn_messages[-1]
    assert reason in (last.content or "") and "".join(shown) == last.content


async def test_a_tool_cancelled_by_stop_leaves_no_unresolved_intent_in_the_journal(tmp_path):
    loop = AgentLoop(
        Router(build_chain([_Silent("stop")], [["m"]]), max_retries=0),
        system_prompt="s",
        permission_mode="yolo",
        cwd=tmp_path,
        session="s",
        reliability=Reliability.from_settings(None, session="s", home=tmp_path / "home"),
    )
    task = asyncio.create_task(loop._execute_tool(ToolCall(id="t1", name="bash", arguments={"command": "sleep 30"})))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    journal = loop.reliability.journal
    assert journal is not None and journal.find_pending(journal.read_all()) == []


def test_a_journal_record_after_a_torn_line_is_not_lost(tmp_path):
    first = ToolJournal(tmp_path, "s")
    first.record_intent("x1", "bash", {"command": "a"}, True)
    first.close()
    with first.path.open("a", encoding="utf-8") as f:
        f.write('{"type": "intent", "id": "x2", "se')  # killed in the middle of a record: no newline
    second = ToolJournal(tmp_path, "s")
    second.record_intent("x3", "bash", {"command": "b"}, True)
    assert [r.id for r in second.find_pending(second.read_all())] == ["x1", "x3"]


# ── what a restarted session tells the model ──────────────────────────


def test_a_call_that_was_cut_off_is_answered_in_the_history_so_the_model_knows_it_started():
    call = {"id": "c1", "name": "bash", "arguments": {"command": "git push"}}
    stored = SimpleNamespace(
        messages=[
            {"role": "system", "content": "S"},
            {"role": "user", "content": "ship it"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
        ]
    )
    history = LiveSession.history.fget(SimpleNamespace(stored=stored))
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert history[-1].tool_call_id == "c1" and history[-1].content == INTERRUPTED_RESULT
    # the wire request keeps the call (it used to be dropped without a word)
    wire = messages_to_openai(history)
    assert [m["role"] for m in wire] == ["user", "assistant", "tool"] and "interrupted" in wire[-1]["content"]


def test_a_call_with_a_result_is_left_alone():
    call = {"id": "c1", "name": "bash", "arguments": {}}
    stored = SimpleNamespace(
        messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "name": "bash", "tool_call_id": "c1", "content": "done"},
        ]
    )
    assert [m.role for m in LiveSession.history.fget(SimpleNamespace(stored=stored))] == ["user", "assistant", "tool"]


# ── the gate, the clients ─────────────────────────────────────────────


def test_a_goal_continuation_is_not_classified_and_planned_again():
    gate = SimpleNamespace(cfg={"plan_first": True, "gate_modes": ["auto"], "gate_unattended": False})
    session = SimpleNamespace(
        background=False,
        scope_override=None,
        goal_continuation=False,
        perms=SimpleNamespace(mode=SimpleNamespace(value="auto")),
    )
    assert PlanFirst.gate_applies(gate, session) is True  # the goal's first turn
    session.goal_continuation = True
    assert PlanFirst.gate_applies(gate, session) is False  # its continuations are the same task


async def test_session_title_sticks_to_the_live_session_and_comes_back(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    res = (await rpc(server, "session.title", {"session_id": sid, "title": "My task"}))["result"]
    assert res["title"] == "My task"
    server.store.save(server.live[sid].stored)  # the next save of the live session used to revert it
    assert server.store.get(sid).title == "My task"


async def test_config_mtime_follows_the_config_file(tmp_path, monkeypatch):
    from k3code.paths import user_config_path

    server, _ = make_server(tmp_path, monkeypatch)
    assert isinstance((await rpc(server, "config.get", {"key": "mtime"}))["result"]["mtime"], float)
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text("max_turns: 5\n")
    assert (await rpc(server, "config.get", {"key": "mtime"}))["result"]["mtime"] > 0.0


async def test_the_status_bar_gets_the_cache_hit_rate_once_there_is_one(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording())
    assert "cache_hit_pct" not in server.context_fields(live)  # nothing sent yet: no rate to show
    live.prompt_tokens_total, live.cache_read_tokens_total = 2000, 500
    assert server.context_fields(live)["cache_hit_pct"] == 25
    await server.close()


async def test_compact_holds_the_turn_lock_so_a_prompt_cannot_start_on_the_old_history(tmp_path, monkeypatch):
    held: list[bool] = []

    class Spy(Recording):
        live = None

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            if "Summarize this coding conversation" in str(messages[0].content):
                held.append(self.live.turn_lock.locked())
            async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
                yield event

    provider = Spy()
    server, live = await seed(tmp_path, monkeypatch, provider, context={"keep_messages": 6})
    provider.live = live
    out = (await cmd(server, "/compact", live.session_id))["output"]
    assert held == [True] and out.startswith("Compacted") and not live.turn_lock.locked()
    await server.close()


def test_a_child_prompt_leaves_out_an_index_of_skills_it_cannot_call(monkeypatch, tmp_path):
    from k3code import prompting
    from k3code.config import Settings

    monkeypatch.setattr(prompting, "skills_prompt", lambda *a, **k: "## Skills\n\n- deploy: ship it")
    cfg = Settings()
    assert "deploy: ship it" in prompting.build_system_prompt("base", cwd=tmp_path, config=cfg)
    assert "deploy: ship it" not in prompting.build_system_prompt("base", cwd=tmp_path, config=cfg, skills=False)


async def test_best_effort_calls_do_not_climb_to_the_main_tier():
    """A title is not worth a call on a pricier tier because the cheap one is down."""
    from k3code.session_ai import make_title

    seen: list[dict] = []

    class Caller:
        async def complete(self, kind, messages, **kw):
            seen.append(kw)
            return SimpleNamespace(text="A title")

    assert await make_title(Caller(), "fix the parser") == "A title"
    assert seen[0]["escalate"] is False
