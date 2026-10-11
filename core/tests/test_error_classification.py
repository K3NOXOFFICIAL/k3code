"""Provider error wordings the classifier must read correctly: overflow vs throttle vs wall, and what survives of an
error body that is not the OpenAI JSON shape."""

from __future__ import annotations

import json

import httpx
import pytest

from k3code.providers.base import ProviderError, error_from_http
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message
from k3code.router.classifier import FailoverReason, classify_api_error

R = FailoverReason


def reason_of(message: str, status: int | None = 400, body: dict | None = None) -> FailoverReason:
    err = ProviderError(message, status_code=status, body=body if body is not None else {"error": {"message": message}})
    return classify_api_error(err, provider="p", model="m").reason


# ── request larger than the whole per-minute bucket ───────────────────────────────────────────────────────────────

TPM_TOO_LARGE = (
    "Request too large for gpt-4o in organization org-x on tokens per min (TPM): Limit 30000, Requested 45678. "
    "The input or output tokens must be reduced in order to run successfully."
)
LEGACY_TOO_LARGE = "Request too large for gpt-4 on tokens per min: Limit: 90000 / min. Requested: 100054."


@pytest.mark.parametrize("message", [TPM_TOO_LARGE, LEGACY_TOO_LARGE])
def test_a_429_whose_request_exceeds_the_bucket_is_overflow_not_a_rate_limit(message):
    # waiting never admits it (the turn used to park for ever); compaction does
    assert reason_of(message, 429, {"error": {"message": message, "code": "rate_limit_exceeded"}}) == R.context_overflow


def test_the_same_429_without_a_status_is_overflow_too():
    assert reason_of(TPM_TOO_LARGE, None) == R.context_overflow


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit reached for gpt-4o on tokens per min (TPM): Limit 30000, Used 28000, Requested 4000. "
        "Please try again in 4s.",
        "Request too large for gpt-4o on tokens per min (TPM): Limit 30000, Requested 20000.",
    ],
)
def test_a_429_that_fits_the_bucket_stays_a_rate_limit(message):
    assert reason_of(message, 429, {"error": {"message": message, "code": "rate_limit_exceeded"}}) == R.rate_limit


# ── overflow wordings of servers that do not use OpenAI's ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "status"),
    [
        ("<400> InternalError.Algo.InvalidParameter: Range of input length should be [1, 98304]", 400),
        ("Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4096. Given: 4000 and 512", 422),
        ("Input validation error: `inputs` must have less than 4095 tokens. Given: 5000", 422),
        ("Requested token count (35000 tokens) exceeds the maximum allowed length (32768 tokens)", 400),
        ("litellm.ContextWindowExceededError: ContextWindowExceededError: input too long", None),
    ],
)
def test_overflow_wordings_of_other_servers_are_recognised(message, status):
    assert reason_of(message, status) == R.context_overflow


def test_a_field_length_error_is_not_overflow():
    assert reason_of("title exceeds the maximum allowed length of 80 characters", 400) == R.bad_request


# ── throttles that contain an overflow phrase ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [None, 400, 413, 422, 500, 502, 503, 529])
def test_bedrock_too_many_tokens_please_wait_is_a_throttle_at_every_status(status):
    message = "ThrottlingException: Too many tokens, please wait before trying again."
    assert reason_of(message, status) == R.rate_limit
    assert reason_of("Too many tokens, please wait before trying again.", status) == R.rate_limit


def test_too_many_tokens_without_the_wait_hint_is_still_overflow():
    assert reason_of("Too many tokens in the prompt: 9000 > 8192", 400) == R.context_overflow


GEMINI_MINUTE = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
    }
}


def test_a_gemini_per_minute_429_is_a_rate_limit_not_a_billing_wall():
    message = GEMINI_MINUTE["error"]["message"]
    assert reason_of(message, 429, GEMINI_MINUTE) == R.rate_limit


def test_a_gemini_daily_or_zero_allowance_429_stays_a_quota_wall():
    for text in (
        "You exceeded your current quota. Quota exceeded for quota id: GenerateContentInputTokensPerDayPerProject",
        "You exceeded your current quota. Quota exceeded for metric: free_tier_input_token_count, limit: 0",
    ):
        body = {"error": {"code": 429, "message": text, "status": "RESOURCE_EXHAUSTED"}}
        assert reason_of(text, 429, body) == R.quota


def test_a_claude_cli_output_limit_is_not_retried_as_a_server_error():
    from k3code.providers.claude_cli import _error_from_result

    err = _error_from_result({"result": "API Error: Claude's response exceeded the 32000 output token maximum."}, "", 1)
    assert err.status_code == 400
    assert classify_api_error(err, provider="p", model="m").reason == R.bad_request


# ── error bodies that are not OpenAI-shaped JSON ──────────────────────────────────────────────────────────────────


def test_a_plain_text_error_body_is_kept_as_the_message():
    raw = b"This model's maximum context length is 8192 tokens, however you requested 9000 tokens"
    err = error_from_http(raw, 400, {}, lambda body: "")
    assert err.message == raw.decode() and err.status_code == 400
    assert classify_api_error(err, provider="p", model="m").reason == R.context_overflow


def test_an_empty_error_body_reads_http_status():
    assert error_from_http(b"", 502, {}, lambda body: "").message == "HTTP 502"


def test_a_long_html_error_page_shows_the_start_and_keeps_the_marker_for_the_classifier():
    page = (
        "<html><head><title>Attention Required! | Cloudflare</title></head>"
        + " " * 600
        + "<body>cf-error-details</body>"
    )
    err = error_from_http(page.encode(), 403, {}, lambda body: "")
    assert len(err.message) <= 500 and "cloudflare" in err.message.lower()
    assert "cf-error-details" in err.body["message"]
    assert classify_api_error(err, provider="p", model="m").reason == R.server


def test_a_json_body_is_still_read_by_the_provider():
    body = json.dumps({"error": {"message": "bad"}}).encode()
    err = error_from_http(body, 400, {"x": "y"}, lambda b: b["error"]["message"])
    assert err.message == "bad" and err.headers == {"x": "y"} and err.body == {"error": {"message": "bad"}}


# ── through the real providers over HTTP ──────────────────────────────────────────────────────────────────────────

MSGS = [Message(role="user", content="hi")]


async def failing_call(status: int, content: bytes, content_type: str) -> ProviderError:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": content_type}, content=content)

    provider = OpenAICompatProvider(
        name="o",
        base_url="https://o.test/v1",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError) as exc:
        _ = [e async for e in provider.stream(MSGS, [], "m")]
    return exc.value


UPSTREAM = "This model's maximum context length is 8192 tokens, however you requested 9000 tokens"


@pytest.mark.parametrize("raw", [json.dumps({"error": {"message": UPSTREAM}}), {"error": {"message": UPSTREAM}}])
async def test_openrouter_forwards_the_upstream_text_in_metadata_raw(raw):
    body = {
        "error": {"message": "Provider returned error", "code": 400, "metadata": {"raw": raw, "provider_name": "x"}}
    }
    err = await failing_call(400, json.dumps(body).encode(), "application/json")
    assert "Provider returned error" in err.message and "maximum context length" in err.message
    assert classify_api_error(err, provider="o", model="m").reason == R.context_overflow


async def test_a_text_plain_overflow_over_http_is_overflow():
    err = await failing_call(400, UPSTREAM.encode(), "text/plain")
    assert err.message == UPSTREAM
    assert classify_api_error(err, provider="o", model="m").reason == R.context_overflow


async def test_a_cloudflare_page_over_http_is_a_server_error_not_an_auth_failure():
    page = b"<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head><body></body></html>"
    err = await failing_call(403, page, "text/html")
    assert classify_api_error(err, provider="o", model="m").reason == R.server
