"""Provider adapter interface and the provider error type the router classifies."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
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


def request_headers(kind: str, api_key: str, *, anthropic_beta: str | None = None) -> dict[str, str]:
    """Headers for one API call. The key goes only here — never into logs."""
    if kind == "anthropic":
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if anthropic_beta:
            headers["anthropic-beta"] = anthropic_beta
        return headers
    return {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}


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
    return ProviderError(message=message, status_code=status, headers=headers, body=body)
