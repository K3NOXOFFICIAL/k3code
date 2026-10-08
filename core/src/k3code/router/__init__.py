"""Provider/model fallback chain: classification, retry, cooldown, walk."""

from k3code.errors import AllProvidersUnreachable, ChainExhausted, ContextOverflow
from k3code.router.classifier import ClassifiedError, FailoverReason, classify_api_error
from k3code.router.cooldown import CooldownStore, EntryCooldown
from k3code.router.router import Router, RouterEvent, build_chain

__all__ = [
    "AllProvidersUnreachable",
    "ChainExhausted",
    "ClassifiedError",
    "ContextOverflow",
    "CooldownStore",
    "EntryCooldown",
    "FailoverReason",
    "Router",
    "RouterEvent",
    "build_chain",
    "classify_api_error",
]
