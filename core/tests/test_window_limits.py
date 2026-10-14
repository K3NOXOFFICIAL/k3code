"""Context-window handling: configured sizes, the estimator the compaction trigger uses, a prompt bigger than the
window, and the output cap sent to each kind of model."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from k3code.config import ConfigError
from k3code.context_budget import FALLBACK_WINDOW, context_window, text_tokens
from k3code.gateway.server import _estimate_tokens
from k3code.providers.base import ProviderError
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message, ToolCall
from k3code.router.classifier import FailoverReason, classify_api_error
from test_auto_compaction import Recording, seed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(200_000, 200_000), ("200k", 200_000), ("1M", 1_000_000), ("32_000", 32_000), (" 128K ", 128_000)],
)
def test_a_hand_written_window_is_read_not_rejected(raw, expected):
    config = SimpleNamespace(models={"unknown-model": {"context_window": raw}})
    assert context_window(config, "unknown-model") == expected


@pytest.mark.parametrize("raw", [[200_000], {"a": 1}, -1, 0, True, 1.5e12, "lots", 12])
def test_an_unusable_window_counts_as_unset_instead_of_failing_every_prompt(raw):
    config = SimpleNamespace(models={"unknown-model": {"context_window": raw}})
    assert context_window(config, "unknown-model") == FALLBACK_WINDOW


def test_config_refuses_a_window_that_is_no_token_count(tmp_path, monkeypatch):
    from k3code.config import Settings
    from k3code.confio import _validate_values

    _validate_values(Settings(models={"m": {"context_window": "200k"}}))
    _validate_values(Settings(models={"m": {"context_window": 128000}}))
    with pytest.raises(ConfigError, match=r"models\.m\.context_window"):
        _validate_values(Settings(models={"m": {"context_window": "big"}}))
    with pytest.raises(ConfigError, match=r"models\.m\.context_window"):
        _validate_values(Settings(models={"m": {"context_window": [1]}}))


def test_tool_call_arguments_are_counted_once_by_the_compaction_estimator():
    from k3code.context_budget import message_tokens

    call = {
        "id": "c1",
        "name": "write",
        "arguments": {"content": "z" * 4000},
        "raw_arguments": json.dumps({"content": "z" * 4000}),
    }
    stored = [{"role": "assistant", "content": None, "tool_calls": [call]}]
    sent = [
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="write", arguments={"content": "z" * 4000}, raw_arguments=call["raw_arguments"])
            ],
        )
    ]
    assert _estimate_tokens(stored) == message_tokens(sent)
    parsed_only = [{"role": "assistant", "content": None, "tool_calls": [{**call, "raw_arguments": None}]}]
    assert _estimate_tokens(parsed_only) < 1.2 * text_tokens("z" * 4000), "the 4000 chars once, not double"


def test_the_estimate_counts_a_cut_off_call_as_the_start_the_model_is_sent():
    from k3code.context_budget import message_tokens, shrink_cut_calls

    raw = "x" * 30_000
    for stored_raw in (raw, None):  # both forms a stored cut-off call can have
        call = {"id": "c1", "name": "write", "arguments": {"_unparsed": raw}, "raw_arguments": stored_raw}
        stored = [{"role": "assistant", "content": None, "tool_calls": [call]}]
        sent = shrink_cut_calls(
            [
                Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(id="c1", name="write", arguments={"_unparsed": raw}, raw_arguments=stored_raw)
                    ],
                )
            ]
        )
        assert _estimate_tokens(stored) == message_tokens(sent) < 200, "not the ~7.5k tokens of the stored text"


async def test_a_prompt_larger_than_the_window_does_not_brick_the_session(tmp_path, monkeypatch):
    # between the follow-up request (~6.0k chars: the system prompt, with its recall line, and the clipped prompt) and
    # the retry of the clipped prompt with its history (~6.4k), which must still overflow
    provider = Recording(overflow_over=6240)
    server, live = await seed(
        tmp_path, monkeypatch, provider, context={"compact_at_tokens": 10_000_000, "keep_messages": 6}
    )
    server.config.models = {server._compaction_model(live): {"context_window": 2000}}  # 2000 tokens: ~1000 chars kept
    giant = "NEWPROMPT " + "g" * 50_000
    status, _ = await server._run_turn(live, giant)
    assert status == "error"
    last = live.stored.messages[-1]
    assert last["role"] == "user" and len(last["content"]) < 2_000
    assert "characters removed" in last["content"] and last["content"].startswith("NEWPROMPT ")
    # the next ordinary prompt fits and is answered
    server.config.models = {}
    status, text = await server._run_turn(live, "short follow-up")
    assert status == "done", (text, live.last_error)
    await server.close()


def test_a_prompt_that_fits_is_left_alone(tmp_path, monkeypatch):
    import asyncio

    async def go():
        server, live = await seed(tmp_path, monkeypatch, Recording())
        live.stored.messages.append({"role": "user", "content": "q" * 500})
        server._clip_unfittable_prompt(live)
        assert live.stored.messages[-1]["content"] == "q" * 500
        await server.close()

    asyncio.run(go())


# ── the output cap per model kind ─────────────────────────────────────────────────────────────────────────────────


def payload_for(model: str) -> dict:
    p = OpenAICompatProvider(
        name="o",
        base_url="https://o.test/v1",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )
    return p._payload([Message(role="user", content="hi")], [], model, max_tokens=4096, temperature=None)


@pytest.mark.parametrize("model", ["o3", "o1-mini", "gpt-5", "gpt-5-mini", "openai/gpt-5"])
def test_openai_reasoning_models_get_max_completion_tokens(model):
    p = payload_for(model)
    assert p["max_completion_tokens"] == 4096 and "max_tokens" not in p


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1", "llama-3.3-70b", "deepseek-chat", "claude-sonnet-5-5", "qwen3"])
def test_other_models_keep_max_tokens(model):
    p = payload_for(model)
    assert p["max_tokens"] == 4096 and "max_completion_tokens" not in p


VLLM = (
    "'max_tokens' or 'max_completion_tokens' is too large: {cap}. This model's maximum context length is {window} "
    "tokens and your request has {used} input tokens ({cap} > {window} - {used})."
)


def reason(message: str) -> FailoverReason:
    err = ProviderError(message, status_code=400, body={"error": {"message": message}})
    return classify_api_error(err, provider="p", model="m").reason


def test_vllm_cap_vs_window_is_overflow_when_the_cap_is_a_small_part_of_the_window():
    # 8192 of a 32768 window with 30000 tokens of input: the input is what compaction can shrink
    assert reason(VLLM.format(cap=8192, window=32768, used=30000)) == FailoverReason.context_overflow


def test_vllm_cap_vs_window_is_a_bad_request_when_the_cap_is_most_of_the_window():
    # 8192 of a 4096 window: this entry cannot take the cap at all, another one can
    assert reason(VLLM.format(cap=8192, window=4096, used=100)) == FailoverReason.bad_request
