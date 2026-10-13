"""Provider adapter interface and the provider error type the router classifies."""

from __future__ import annotations

import abc
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from k3code.providers.types import Message, StreamEvent, ToolSpec


@dataclass
class ProviderError(Exception):
    """An HTTP/API failure raised by a provider adapter.

    Carries everything ``error_classifier`` needs: status code, response
    headers, structured body. Never includes the API key.
    """

    message: str
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


#: characters of a non-JSON error body shown in the message / kept for the classifier (challenge-page markers sit deep)
ERROR_TEXT_SHOWN = 500
ERROR_TEXT_KEPT = 4000


def error_from_http(
    raw: bytes, status_code: int, headers: dict[str, str], message_of: Callable[[dict[str, Any]], str]
) -> ProviderError:
    """The ``ProviderError`` for an HTTP error response. A JSON body is read by ``message_of``; a plain-text or HTML one
    (a gateway, a local server, a CDN page) is the only diagnostic there is, so its text is kept: the message shows
    the start of it and the classifier gets more of it, so an overflow or a WAF marker is still recognised."""
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    if isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict):
        body = body[0]  # Google's endpoints wrap the error in a one-element array: [{"error": {...}}]
    if not isinstance(body, dict):
        body = {"message": str(body)}
    message = message_of(body)
    if not message and not body:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            message = " ".join(text[:ERROR_TEXT_SHOWN].split())
            body = {"message": text[:ERROR_TEXT_KEPT]}
    return ProviderError(
        message=message or f"HTTP {status_code}", status_code=status_code, headers=dict(headers), body=body
    )


class Provider(abc.ABC):
    """One provider entry: a base_url, a credential and a chat API flavor."""

    name: str
    base_url: str
    api_key: str

    @abc.abstractmethod
    def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        *,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one completion; yields text deltas, tool calls and a final done event."""

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Release the underlying HTTP client."""


def redact(value: str | None) -> str:
    """Guard against ever logging a key: collapses it to a marker."""
    if not value:
        return ""
    return "***" if len(value) <= 4 else f"***{value[-2:]}"


def request_headers(
    kind: str, api_key: str, *, keyless: bool = False, anthropic_beta: str | None = None
) -> dict[str, str]:
    """Headers for one API call. The key goes only here — never into logs. A ``keyless`` entry (``api_key_env: ""``,
    e.g. Ollama) sends no key header; for any other entry an empty key is a configuration error."""
    has_key = bool(api_key.strip())
    if not has_key and not keyless:
        raise ProviderError(
            "No API key configured for this provider: its api_key_env variable is empty. "
            "Run `k3code setup --step providers`, or put KEY=value in ~/.config/k3code/env. "
            "`k3code doctor` shows which variable is missing.",
            status_code=401,
        )
    if kind == "anthropic":
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if has_key:
            headers["x-api-key"] = api_key
        if anthropic_beta:
            headers["anthropic-beta"] = anthropic_beta
        return headers
    headers = {"content-type": "application/json"}
    if has_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def http_status_of(exc: BaseException) -> int | None:
    """HTTP status from an httpx exception, when it carries one."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def headers_of(exc: BaseException) -> dict[str, str]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    try:
        return dict(headers)
    except Exception:  # pragma: no cover - defensive
        return {}


def to_provider_error(exc: BaseException, *, kind: str) -> ProviderError:
    """Convert an httpx (or other transport) exception into a ProviderError."""
    if isinstance(exc, ProviderError):
        return exc
    status = http_status_of(exc)
    headers = headers_of(exc)
    body: dict[str, Any] = {}
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}
    type_name = type(exc).__name__
    message = f"{type_name}: {exc}" if str(exc) else type_name
    err = ProviderError(message=message, status_code=status, headers=headers, body=body)
    err.__cause__ = exc  # the classifier reads the transport type from the chain
    return err
