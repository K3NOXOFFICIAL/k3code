"""Core error types shared by the router, providers and agent loop."""

from __future__ import annotations


class K3CodeError(Exception):
    """Base class for k3code errors."""


class ContextOverflow(K3CodeError):
    """The request exceeded the model's context window; the loop should compact."""


class AllProvidersUnreachable(K3CodeError):
    """Every provider/model entry failed with network errors.

    The M2 offline-pause layer catches this to pause and auto-resume work.
    """

    def __init__(self, message: str = "all provider entries are unreachable", attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


class ChainExhausted(K3CodeError):
    """Every entry in the fallback chain failed and the failure is not retryable as a whole."""

    def __init__(self, message: str, last_reason: str = "unknown") -> None:
        super().__init__(message)
        self.last_reason = last_reason


class PermissionDenied(K3CodeError):
    """A tool call was denied by the permission policy."""
