"""Per-turn reasoning effort (``/effort``), passed to the providers that take one.

The gateway sets :data:`REASONING_EFFORT` for the turn's task; providers read it when they build a request, so the
router and retry layers in between need no extra parameter.
"""

from __future__ import annotations

import re
from contextvars import ContextVar

LEVELS = ("low", "medium", "high", "xhigh", "max")

REASONING_EFFORT: ContextVar[str | None] = ContextVar("k3code_reasoning_effort", default=None)

# Claude models that accept ``output_config.effort``: every 5-generation model, Opus 4.5+ and Sonnet 4.6.
_ANTHROPIC_EFFORT = re.compile(r"claude-(?:[a-z]+-5(?:\b|-)|opus-4-[5-9]|sonnet-4-6)")
# OpenAI reasoning models take ``reasoning_effort`` (low|medium|high).
_OPENAI_EFFORT = re.compile(r"(?:^|/)(?:o\d|gpt-5)")


def current() -> str | None:
    effort = REASONING_EFFORT.get()
    return effort if effort in LEVELS else None


def takes_effort(model: str, kind: str) -> bool | None:
    """Whether a request to ``model`` through a provider of ``kind`` carries the session's effort level.

    The same test the providers apply when they build a request; None when there is nothing to judge (no model id,
    or a provider kind this module knows nothing about). The claude-cli provider sends no effort at all.
    """
    if not model:
        return None
    if kind == "anthropic":
        return bool(_ANTHROPIC_EFFORT.search(model.lower()))
    if kind == "openai":
        return bool(_OPENAI_EFFORT.search(model.lower()))
    if kind == "claude-cli":
        return False
    return None


def anthropic_effort(model: str) -> str | None:
    effort = current()
    return effort if effort and _ANTHROPIC_EFFORT.search(model.lower()) else None


def openai_effort(model: str) -> str | None:
    effort = current()
    if not effort or not _OPENAI_EFFORT.search(model.lower()):
        return None
    return "high" if effort in ("xhigh", "max") else effort
