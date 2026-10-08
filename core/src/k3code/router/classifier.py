# Vendored from hermes-agent@4127d78da84b1eee105f298979cc57cc7457f98d:agent/error_classifier.py (MIT)
# Copyright (c) 2025 Nous Research
# Adapted for k3code: the FailoverReason enum is collapsed to the nine reasons the
# k3code router acts on, and only the lowercased-substring pattern tables and the
# status/message/transport dispatch shape are carried over. Every Hermes-specific
# stage (plugin hooks, provider profiles, Nous welcome tier, MoA, Codex replay,
# image/reasoning recovery) is dropped along with its tables.

"""API error classification for the k3code fallback chain.

``classify_api_error`` maps a provider exception to a :class:`ClassifiedError`
whose ``reason`` tells the router what to do: retry the same entry, fail over to
the next one, compact, or fail.
"""

from __future__ import annotations

import enum
import json
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

# ── Error taxonomy ──────────────────────────────────────────────────────


class FailoverReason(enum.Enum):
    """Why an API call failed — determines the router's recovery strategy."""

    rate_limit = "rate_limit"              # 429 / throttling — backoff, then fail over
    quota = "quota"                        # credits or plan exhausted — fail over immediately
    auth = "auth"                          # 401/403 bad or missing key — fail over immediately
    context_overflow = "context_overflow"  # prompt too large — raise ContextOverflow, compact
    network = "network"                    # connect/DNS failure — retry, then fail over
    server = "server"                      # 5xx / overloaded — retry, then fail over
    timeout = "timeout"                    # read/connect timeout — retry, then fail over
    bad_request = "bad_request"            # malformed request/model — fail over, no retry
    unknown = "unknown"                    # unclassifiable — retry with backoff, then fail over


#: Reasons for which the router retries the same entry before failing over.
RETRYABLE_REASONS = frozenset(
    {FailoverReason.network, FailoverReason.timeout, FailoverReason.server,
     FailoverReason.rate_limit, FailoverReason.unknown}
)

#: Reasons that skip retries and move straight to the next entry. ``bad_request``
#: (e.g. an unknown model id) is entry-specific — identical on retry, but a
#: different model/provider down the chain may well work — so it fails over
#: rather than aborting the walk.
IMMEDIATE_FAILOVER_REASONS = frozenset(
    {FailoverReason.auth, FailoverReason.quota, FailoverReason.bad_request}
)

#: Reasons that abort the whole walk instead of trying the next entry:
#: a context overflow will recur on every entry until the loop compacts.
TERMINAL_REASONS = frozenset({FailoverReason.context_overflow})


@dataclass
class ClassifiedError:
    """Structured classification of one API failure."""

    reason: FailoverReason
    status_code: int | None = None
    provider: str = ""
    model: str = ""
    message: str = ""
    #: Seconds until the provider says its window reopens (Retry-After / body / prose).
    retry_after: float | None = None
    error_context: dict[str, Any] = field(default_factory=dict)

    @property
    def retryable(self) -> bool:
        return self.reason in RETRYABLE_REASONS

    @property
    def immediate_failover(self) -> bool:
        return self.reason in IMMEDIATE_FAILOVER_REASONS

    @property
    def terminal(self) -> bool:
        return self.reason in TERMINAL_REASONS


# ── Pattern tables (lowercased substrings) ──────────────────────────────
# Carried over from Hermes' tables; ``billing`` there maps to ``quota`` here.

_QUOTA_PATTERNS = (
    "insufficient credits", "insufficient_quota", "insufficient balance", "credit balance",
    "credits exhausted", "credits have been exhausted", "requires available credits",
    "account balance is too low", "no usable credits", "top up your credits", "payment required",
    "billing hard limit", "exceeded your current quota", "account is deactivated", "plan does not include",
    "out of extra usage", "out of funds", "run out of funds", "balance_depleted",
    "budget limit exceeded", "hard billing limit",
    "model_not_supported_on_free_tier", "not available on the free tier",
    "key limit exceeded", "spending limit",
    "reached its daily usage quota", "daily usage quota", "usage quota",  # OmniRoute per-key quota (HTTP 400/429)
)

_QUOTA_ERROR_CODES = frozenset({
    "insufficient_quota", "billing_not_active", "payment_required", "insufficient_credits",
    "no_usable_credits", "balance_depleted", "model_not_supported_on_free_tier",
    "member_spend_cap_exceeded", "terminal_quota_exhausted",
    "personal-team-blocked:spending-limit",
    "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "organization_usage_limit_exceeded", "project_spend_limit_exceeded",
    "insufficient_credits_for_paid_model", "usage_limit_exceeded",
})

_RATE_LIMIT_PATTERNS = (
    "rate limit", "rate_limit", "too many requests", "throttled", "requests per minute",
    "tokens per minute", "requests per day", "try again in", "please retry after",
    "resource exhausted", "resource_exhausted", "resource-exhausted", "resourceexhausted",
    "rate increased too quickly", "throttlingexception", "too many concurrent requests",
    "servicequotaexceededexception", "throttling",
)

# Server busy, credential fine: back off on the same entry. Z.AI/Zhipu reuse
# HTTP 429 for this, so the 429 path checks these first.
_OVERLOADED_PATTERNS = (
    "overloaded", "temporarily overloaded", "service is temporarily overloaded",
    "service may be temporarily overloaded", "server is overloaded", "server overloaded",
    "server overload", "server_overload",
    "service overloaded", "service is overloaded", "upstream overloaded", "currently overloaded",
    "upstream model provider is temporarily unavailable. please try again in a moment.",
    "at capacity", "over capacity",
)

# Usage-limit phrases needing disambiguation (quota OR rate_limit), and the
# signals that mark such a limit as a periodic window rather than a hard wall.
_USAGE_LIMIT_PATTERNS = ("usage limit", "quota", "limit exceeded", "key limit exceeded")
_USAGE_LIMIT_TRANSIENT_SIGNALS = (
    "try again", "retry", "resets at", "reset in", "resets in", "reset after", "available in",
    "wait", "requests remaining", "periodic", "window", "per minute", "per second",
)

_CONTEXT_OVERFLOW_PATTERNS = (
    "context length", "context size", "maximum context", "token limit", "too many tokens",
    "reduce the length", "exceeds the limit", "context window", "prompt is too long",
    "prompt exceeds max length", "maximum number of tokens",
    "exceeds the max_model_len", "max_model_len", "prompt length", "input is too long",
    "maximum model length", "context length exceeded", "truncating input",
    "slot context", "n_ctx_slot",
    "超过最大长度", "上下文长度",
    "tokens in request more than max tokens allowed",
    "max input token", "input token", "exceeds the maximum number of input tokens",
    "maximum allowed input length",
    # 413 wordings land here too: k3code has no separate payload-compression path in M0.
    "request entity too large", "payload too large", "error code: 413", "request_too_large",
    "request exceeds the maximum size",
)

# A rejected *output* cap ("max_tokens: 8192 > 4096, the maximum allowed number of output tokens for this model",
# "max_tokens is too large ...") is about this one entry, not about the conversation: another entry can serve the
# request, and compaction cannot help. Classified as overflow it aborted the whole fallback walk on every tick.
_OUTPUT_CAP_PATTERNS = ("max_tokens", "max_completion_tokens", "output tokens", "output token limit")

_CONTEXT_OVERFLOW_ERROR_CODES = frozenset({"context_length_exceeded", "max_tokens_exceeded"})

_MODEL_NOT_FOUND_PATTERNS = (
    "is not a valid model", "invalid model", "model not found", "model_not_found", "does not exist",
    "no such model", "unknown model", "unsupported model", "no endpoints found that support tool use",
)

_MODEL_NOT_FOUND_ERROR_CODES = frozenset({"model_not_found", "model_not_available", "invalid_model"})

# Deterministic rejections of the request shape: identical on every retry.
_REQUEST_VALIDATION_PATTERNS = (
    "unknown parameter", "unsupported parameter", "unrecognized request argument",
    "invalid_request_error", "unknown_parameter", "unsupported_parameter",
)

_INVALID_MESSAGE_BODY_PATTERNS = (
    "must have non-empty content", "messages must have non-empty", "invalid_request_body",
    "text content blocks must be non-empty", "content field is required",
    "messages: at least one message is required", "no user query found",
)

_AUTH_PATTERNS = (
    "invalid api key", "invalid_api_key", "gateway_auth_failed", "authentication", "unauthorized",
    "forbidden", "invalid token", "token expired", "token revoked", "access denied",
    "failed to extract accountid from token",
)

_EMPTY_PROVIDER_RESPONSE_PATTERNS = (
    "returned an empty response", "empty response despite retries", "provider returned an empty response",
    "model returning empty responses", "empty response stream",
)

_TIMEOUT_MESSAGE_PATTERNS = (
    "timed out", "turn timed out", "request timed out", "deadline exceeded", "operation timed out",
    "upstream timed out",
)

# Connect/DNS failures with no status. EXCLUDES mid-stream disconnects.
_CONNECTION_MESSAGE_PATTERNS = (
    "connection refused", "econnrefused", "no route to host", "network is unreachable",
    "network unreachable", "name or service not known", "temporary failure in name resolution",
    "nodename nor servname provided", "getaddrinfo failed", "getaddrinfo enotfound", "eai_again",
    "fetch failed", "failed to fetch", "upstream connect error",
    "all connection attempts failed",
)

_SERVER_DISCONNECT_PATTERNS = (
    "server disconnected", "peer closed connection", "connection reset by peer", "connection was closed",
    "network connection lost", "unexpected eof", "incomplete chunked read",
)

_SSL_CERT_VERIFY_PATTERNS = (
    "certificate verify failed", "certificate_verify_failed", "unable to get local issuer certificate",
    "self-signed certificate", "self signed certificate", "certificate has expired",
    "hostname mismatch, certificate is not valid", "unable to verify the first certificate",
)

_SSL_TRANSIENT_PATTERNS = (
    "bad record mac", "ssl alert", "tls alert", "ssl handshake failure", "tlsv1 alert", "sslv3 alert",
    "bad_record_mac", "ssl_alert", "tls_alert", "tls_alert_internal_error", "[ssl:",
)

# A 403 written by a WAF/CDN rather than the provider's API: the credential never
# reached the provider, so another entry (another host) can still work.
_UPSTREAM_BLOCKED_PATTERNS = (
    "your request was blocked", "request blocked", "sorry, you have been blocked",
    "enable javascript and cookies to continue", "cdn-cgi/challenge-platform", "cf-browser-verification",
    "challenge-error-text", "__cf_chl", "cf-error-details", "attention required! | cloudflare",
)
#: the same challenge markers, public for the browser tool, which escalates a page that shows one (research/browser.py)
UPSTREAM_BLOCKED_PATTERNS = _UPSTREAM_BLOCKED_PATTERNS

# httpx / stdlib transport exception type names. A connect failure is ``network``;
# a read/pool timeout is ``timeout``.
_NETWORK_ERROR_TYPES = frozenset({
    "ConnectError", "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "ConnectionAbortedError", "BrokenPipeError", "RemoteProtocolError", "ReadError", "WriteError",
    "NetworkError", "ProxyError", "UnsupportedProtocol", "ServerDisconnectedError",
    "APIConnectionError",
})

_TIMEOUT_ERROR_TYPES = frozenset({
    "ReadTimeout", "ConnectTimeout", "PoolTimeout", "WriteTimeout", "TimeoutException",
    "TimeoutError", "APITimeoutError",
})

_SSL_ERROR_TYPES = frozenset({
    "SSLError", "SSLZeroReturnError", "SSLWantReadError", "SSLWantWriteError", "SSLEOFError",
    "SSLSyscallError", "SSLCertVerificationError",
})

_RESET_FIELDS = ("resets_in_seconds", "resets_at", "reset_at", "retry_after")
_RESET_HEADERS = ("retry-after", "Retry-After", "x-ratelimit-reset", "X-RateLimit-Reset")

_R = FailoverReason

# ── Rule tables: ordered (patterns, reason) pairs, matched first-hit ────

_MESSAGE_RULES: tuple[tuple[Sequence[str], FailoverReason], ...] = (
    (_OVERLOADED_PATTERNS, _R.server),
    (_QUOTA_PATTERNS, _R.quota),
    (_RATE_LIMIT_PATTERNS, _R.rate_limit),
    (_EMPTY_PROVIDER_RESPONSE_PATTERNS, _R.server),
    (_CONTEXT_OVERFLOW_PATTERNS, _R.context_overflow),
    (_AUTH_PATTERNS, _R.auth),
    (_MODEL_NOT_FOUND_PATTERNS, _R.bad_request),
    (_TIMEOUT_MESSAGE_PATTERNS, _R.timeout),
    (_CONNECTION_MESSAGE_PATTERNS, _R.network),
)

_5XX_RULES: tuple[tuple[Sequence[str], FailoverReason], ...] = (
    (_EMPTY_PROVIDER_RESPONSE_PATTERNS, _R.server),
    (_CONTEXT_OVERFLOW_PATTERNS, _R.context_overflow),
)

_404_RULES: tuple[tuple[Sequence[str], FailoverReason], ...] = (
    (_QUOTA_PATTERNS, _R.quota),
    (_MODEL_NOT_FOUND_PATTERNS, _R.bad_request),
)

_ERROR_CODE_REASONS: dict[str, FailoverReason] = {
    **dict.fromkeys(("resource_exhausted", "throttled", "rate_limit_exceeded"), _R.rate_limit),
    **dict.fromkeys(_QUOTA_ERROR_CODES, _R.quota),
    **dict.fromkeys(_MODEL_NOT_FOUND_ERROR_CODES, _R.bad_request),
    **dict.fromkeys(_CONTEXT_OVERFLOW_ERROR_CODES, _R.context_overflow),
    "server_error": _R.server,
    "api_error": _R.server,
    "rate_limit_error": _R.rate_limit,
    "unavailable": _R.server,
    "internal": _R.server,
    "deadline_exceeded": _R.timeout,
    "invalid_request_body": _R.bad_request,
}


def _first_match(msg: str, rules: Sequence[tuple[Sequence[str], FailoverReason]]) -> FailoverReason | None:
    """Reason of the first rule whose pattern list hits ``msg``."""
    for patterns, reason in rules:
        if any(p in msg for p in patterns):
            return reason
    return None


# ── Classification context ──────────────────────────────────────────────


@dataclass
class _Ctx:
    """Everything the classifier stages need about one failed call."""

    error: BaseException
    status_code: int | None
    body: dict[str, Any]
    msg: str  # lowercased str(error) + body message(s)
    headers: dict[str, str]
    provider: str
    model: str

    def __post_init__(self) -> None:
        self.error_type = type(self.error).__name__
        self.code = _extract_error_code(self.body).lower()


# ── Stages ──────────────────────────────────────────────────────────────


def _by_status(c: _Ctx) -> FailoverReason | None:
    """HTTP status with message-aware refinement; unlisted 4xx/5xx get a generic verdict."""
    status = c.status_code
    if status is None:
        return None
    if status == 400:
        return _status_400(c)
    if status == 401:
        return _R.auth
    if status == 402:
        return _status_402(c)
    if status == 403:
        return _status_403(c)
    if status == 404:
        return _status_404(c)
    if status == 408:
        return _R.timeout
    if status in (413, 422):
        return _first_match(c.msg, _5XX_RULES) or (_R.context_overflow if status == 413 else _R.bad_request)
    if status == 429:
        return _status_429(c)
    if status in (500, 502):
        return _status_5xx(c)
    if status in (503, 529):
        return _first_match(c.msg, _5XX_RULES) or _R.server
    if 400 <= status < 500:
        return _R.bad_request
    if 500 <= status < 600:
        return _R.server
    return None


def _status_400(c: _Ctx) -> FailoverReason:
    """400 Bad Request — request-shape rejections, overflow, quota/rate relabels, or generic."""
    msg, code = c.msg, c.code
    # Before overflow: a rejected ``max_tokens`` parameter contains an overflow phrase.
    if any(p in msg for p in _REQUEST_VALIDATION_PATTERNS if p != "invalid_request_error"):
        return _R.bad_request
    if code in {"unknown_parameter", "unsupported_parameter"}:
        return _R.bad_request
    if code not in _CONTEXT_OVERFLOW_ERROR_CODES and any(p in msg for p in _OUTPUT_CAP_PATTERNS):
        return _R.bad_request
    # A malformed message array is not overflow: the input can be tiny and
    # compaction cannot fix it.
    if any(p in msg for p in _INVALID_MESSAGE_BODY_PATTERNS) or code == "invalid_request_body":
        return _R.bad_request
    if code in _CONTEXT_OVERFLOW_ERROR_CODES:
        return _R.context_overflow
    refined = _first_match(
        msg,
        (
            (_CONTEXT_OVERFLOW_PATTERNS, _R.context_overflow),
            (_MODEL_NOT_FOUND_PATTERNS, _R.bad_request),
            (_RATE_LIMIT_PATTERNS, _R.rate_limit),
            (_QUOTA_PATTERNS, _R.quota),
        ),
    )
    return refined or _R.bad_request


def _status_402(c: _Ctx) -> FailoverReason:
    """402: "usage limit, try again in 5 minutes" is a periodic quota, not a billing wall."""
    transient = any(p in c.msg for p in _USAGE_LIMIT_PATTERNS) and any(
        p in c.msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS
    )
    return _R.rate_limit if transient else _R.quota


def _status_403(c: _Ctx) -> FailoverReason:
    if any(p in c.msg for p in _QUOTA_PATTERNS):
        return _R.quota
    # A WAF/CDN answered, not the provider: the key never reached it, so another
    # entry (another host) is the right recovery, not a key fix.
    if any(p in c.msg for p in _UPSTREAM_BLOCKED_PATTERNS):
        return _R.server
    return _R.auth


def _status_404(c: _Ctx) -> FailoverReason:
    if c.code in _QUOTA_ERROR_CODES:
        return _R.quota
    return _first_match(c.msg, _404_RULES) or _R.bad_request


def _status_429(c: _Ctx) -> FailoverReason:
    # A structured quota code is decisive: a hard cap, not throttling.
    if c.code in _QUOTA_ERROR_CODES:
        return _R.quota
    # Z.AI/Zhipu and others reuse 429 for server-wide overload: back off here.
    if any(p in c.msg for p in _OVERLOADED_PATTERNS):
        return _R.server
    # Quota walls arriving as 429 are quota ONLY when the body is not itself a
    # rate-limit phrase and names no reset window.
    quota_wall = c.code == "usage_limit_reached" or any(
        p in c.msg for p in ("usage_limit_reached",) + _USAGE_LIMIT_PATTERNS + _QUOTA_PATTERNS
    )
    explicit_rate_limit = any(p in c.msg for p in _RATE_LIMIT_PATTERNS)
    if quota_wall and not explicit_rate_limit and not _has_transient_signal(c):
        return _R.quota
    return _R.rate_limit


def _status_5xx(c: _Ctx) -> FailoverReason:
    # Request-validation errors arriving as 5xx fail fast instead of retry-flooding.
    if any(p in c.msg for p in _REQUEST_VALIDATION_PATTERNS):
        return _R.bad_request
    return _first_match(c.msg, _5XX_RULES) or _R.server


def _by_error_code(c: _Ctx) -> FailoverReason | None:
    return _ERROR_CODE_REASONS.get(c.code)


def _by_message(c: _Ctx) -> FailoverReason | None:
    """Message patterns when no status settled it; status-less usage limits get the 402 split."""
    if any(p in c.msg for p in _USAGE_LIMIT_PATTERNS) and not any(
        p in c.msg for p in _OVERLOADED_PATTERNS + _CONTEXT_OVERFLOW_PATTERNS
    ):
        return _status_402(c)
    return _first_match(c.msg, _MESSAGE_RULES)


def _by_transport(c: _Ctx) -> FailoverReason | None:
    """SSL, disconnect and transport-type heuristics, in that order."""
    msg = c.msg
    # A cert failure is deterministic (fail the request shape); a transient TLS
    # alert is worth a retry. Checked before disconnects: both contain "[ssl:".
    if any(p in msg for p in _SSL_CERT_VERIFY_PATTERNS):
        return _R.network
    if any(p in msg for p in _SSL_TRANSIENT_PATTERNS) or c.error_type in _SSL_ERROR_TYPES:
        return _R.network
    if any(p in msg for p in _SERVER_DISCONNECT_PATTERNS) and not c.status_code:
        return _R.server
    if c.error_type in _TIMEOUT_ERROR_TYPES or isinstance(c.error, TimeoutError):
        return _R.timeout
    if c.error_type in _NETWORK_ERROR_TYPES or isinstance(c.error, (ConnectionError, OSError)):
        return _R.network
    return None


_STAGES = (_by_status, _by_error_code, _by_message, _by_transport)


def classify_api_error(
    error: BaseException,
    *,
    provider: str = "",
    model: str = "",
) -> ClassifiedError:
    """Classify a provider failure into one :class:`FailoverReason` plus a retry window.

    The API key is never read and never appears in the result.
    """
    status_code = _extract_status_code(error)
    body = _extract_error_body(error)
    headers = _extract_headers(error)
    c = _Ctx(
        error=error,
        status_code=status_code,
        body=body,
        msg=_build_error_msg(error, body),
        headers=headers,
        provider=provider,
        model=model,
    )
    reason = next((r for r in (stage(c) for stage in _STAGES) if r is not None), _R.unknown)
    retry_after = None
    if reason in (_R.rate_limit, _R.quota, _R.server):
        retry_after = _reset_seconds(c)
    return ClassifiedError(
        reason=reason,
        status_code=status_code,
        provider=provider,
        model=model,
        message=_extract_message(error, body),
        retry_after=retry_after,
    )


# ── Helpers ─────────────────────────────────────────────────────────────


def _has_transient_signal(c: _Ctx) -> bool:
    """Whether a usage-limit response identifies a reset window (message, body or headers)."""
    if any(p in c.msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS):
        return True
    payloads = [p for p in (c.body, _error_obj(c.body)) if isinstance(p, dict)]
    if any(payload.get(f) not in (None, "") for payload in payloads for f in _RESET_FIELDS):
        return True
    return any(c.headers.get(h) not in (None, "") for h in _RESET_HEADERS)


def _reset_seconds(c: _Ctx) -> float | None:
    """Seconds until the window reopens, from body reset fields, ``Retry-After``, or prose."""
    from k3code.providers.retry_utils import parse_retry_after_seconds, reset_delay_from_message

    for payload in (p for p in (c.body, _error_obj(c.body)) if isinstance(p, dict)):
        for name in _RESET_FIELDS:
            value = payload.get(name)
            if value in (None, ""):
                continue
            if name.endswith("_at") and isinstance(value, (int, float)):
                return max(0.0, float(value) - time.time())
            if (seconds := parse_retry_after_seconds(value)) is not None:
                return seconds
    if (seconds := parse_retry_after_seconds(c.headers)) is not None:
        return seconds
    return reset_delay_from_message(c.msg)


def _error_obj(body: Any) -> dict[str, Any]:
    """``body["error"]`` when it is a dict, else ``{}``."""
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {}


def _json_dict(text: Any) -> dict[str, Any] | None:
    if not (isinstance(text, str) and text.strip()):
        return None
    try:
        inner = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return inner if isinstance(inner, dict) else None


def _build_error_msg(error: BaseException, body: Any) -> str:
    """Lowercased str(error) plus the body message, deduplicated."""
    raw_msg = str(error).lower()
    body_msg = ""
    if isinstance(body, dict):
        err_obj = _error_obj(body)
        body_msg = str(err_obj.get("message") or "").lower() or str(body.get("message") or "").lower()
    parts = [raw_msg]
    if body_msg and body_msg not in raw_msg:
        parts.append(body_msg)
    return " ".join(parts)


def _body_message_candidates(body: dict[str, Any]) -> Iterator[Any]:
    """Body message fields in priority order (OpenAI, flat, proxy, FastAPI shapes)."""
    yield _error_obj(body).get("message")
    yield body.get("message")
    yield body.get("errorMessage")
    detail = body.get("detail")
    yield detail.get("message") if isinstance(detail, dict) else detail if isinstance(detail, str) else None


def _from_cause_chain(error: BaseException, pick, default):
    """First non-None ``pick(exc)`` over the error and its cause chain (max 5 deep)."""
    current: BaseException | None = error
    for _ in range(5):
        if current is None:
            break
        found = pick(current)
        if found is not None:
            return found
        cause = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if cause is None or cause is current:
            break
        current = cause
    return default


def _status_of(exc: Any) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int) and 100 <= code < 600:
        return code
    code = getattr(exc, "status", None)
    return code if isinstance(code, int) and 100 <= code < 600 else None


def _body_of(exc: Any) -> dict[str, Any] | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(exc, "response", None)
    try:
        json_body = response.json() if response is not None else None
    except Exception:
        return None
    return json_body if isinstance(json_body, dict) else None


def _headers_of(exc: Any) -> dict[str, str] | None:
    headers = getattr(exc, "headers", None)
    if isinstance(headers, dict) and headers:
        return headers
    response_headers = getattr(getattr(exc, "response", None), "headers", None)
    if response_headers is not None and hasattr(response_headers, "get"):
        try:
            return dict(response_headers)
        except Exception:
            return None
    return None


def _extract_status_code(error: BaseException) -> int | None:
    return _from_cause_chain(error, _status_of, None)


def _extract_error_body(error: BaseException) -> dict[str, Any]:
    return _from_cause_chain(error, _body_of, {})


def _extract_headers(error: BaseException) -> dict[str, str]:
    return _from_cause_chain(error, _headers_of, {}) or {}


def _extract_error_code(body: dict[str, Any]) -> str:
    """Code/type from ``body.error`` or a top-level key; ``"400"`` is not a code."""
    if not body:
        return ""
    error_obj = body.get("error", {})
    if isinstance(error_obj, dict):
        code = error_obj.get("code") or error_obj.get("type") or ""
        if not isinstance(code, str):
            code = error_obj.get("status") or ""
        if isinstance(code, str) and code.strip() and code.strip() != "400":
            return code.strip()
        message = error_obj.get("message")
        if isinstance(message, str) and message.strip().startswith("{"):
            nested = _json_dict(message) or {}
            nested_code = nested.get("code") or nested.get("error_code") or ""
            if isinstance(nested_code, str) and nested_code.strip():
                return nested_code.strip()
    for key in ("code", "error_code", "errorCode"):
        value = body.get(key)
        if isinstance(value, (str, int)) and str(value).strip() not in ("", "400"):
            return str(value).strip()
    return ""


def _extract_message(error: BaseException, body: dict[str, Any]) -> str:
    """The most informative error message (structured body first), capped at 500 chars."""
    msg = next((m for m in _body_message_candidates(body or {}) if isinstance(m, str) and m.strip()), None)
    return (msg.strip() if msg else str(error))[:500]


_WHITESPACE_RE = re.compile(r"\s+")


def summarize(error: BaseException, limit: int = 160) -> str:
    """One-line error summary for event logs. Never includes credentials."""
    text = _WHITESPACE_RE.sub(" ", f"{type(error).__name__}: {error}").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
