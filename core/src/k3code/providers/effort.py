"""Per-turn reasoning effort (``/effort``), passed to the providers that take one.

The gateway sets :data:`REASONING_EFFORT` for the turn's task; providers read it when they build a request, so the
router and retry layers in between need no extra parameter.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar

LEVELS = ("low", "medium", "high", "xhigh", "max")

logger = logging.getLogger(__name__)

REASONING_EFFORT: ContextVar[str | None] = ContextVar("k3code_reasoning_effort", default=None)

# Claude models that accept ``output_config.effort``: every 5-generation model, Opus 4.5+ and Sonnet 4.6.
_ANTHROPIC_EFFORT = re.compile(r"claude-(?:[a-z]+-5(?:\b|-)|opus-4-[5-9]|sonnet-4-6)")
# The ones with fewer levels (the Claude API docs): Opus 4.5 takes low|medium|high; Opus 4.6 and Sonnet 4.6 add max but
# not xhigh, which arrived with Opus 4.7. Every other model above takes all five.
_NO_MAX = re.compile(r"claude-opus-4-5(?!\d)")
_NO_XHIGH = re.compile(r"claude-(?:opus|sonnet)-4-6(?!\d)")
# Models that answer a ``temperature`` with HTTP 400: the Claude 5 generation and Opus 4.7+ (sampling parameters were
# removed; Sonnet 5.5 and Haiku 5.5 take only the default).
_NO_TEMPERATURE = re.compile(r"claude-(?:[a-z]+-5(?:\b|-)|opus-4-[7-9])")
_WARNED_TEMPERATURE: set[str] = set()
# OpenAI reasoning models take ``reasoning_effort`` (low|medium|high).
_OPENAI_EFFORT = re.compile(r"(?:^|/)(?:o\d|gpt-5)")


def _claude_id(model: str) -> str:
    """The lower-cased model id with a dotted version spelled like the API's (``claude-opus-4.7`` is ``-4-7``): relays
    and gateways use both."""
    return re.sub(r"(?<=\d)\.(?=\d)", "-", model.lower())


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
        return bool(_ANTHROPIC_EFFORT.search(_claude_id(model)))
    if kind == "openai":
        return bool(_OPENAI_EFFORT.search(model.lower()))
    if kind == "claude-cli":
        return False
    return None


def anthropic_effort(model: str) -> str | None:
    """The session's effort level as ``model`` takes it: a level the model does not have becomes the nearest one below
    (xhigh is max where max exists, else high; max is high), which the API would otherwise answer with HTTP 400."""
    effort = current()
    model = _claude_id(model)
    if not effort or not _ANTHROPIC_EFFORT.search(model):
        return None
    if effort == "xhigh" and _NO_XHIGH.search(model):
        return "max"
    if effort in ("xhigh", "max") and _NO_MAX.search(model):
        return "high"
    return effort


def usable_temperature(model: str, temperature: float | None) -> float | None:
    """``temperature``, or None for a Claude model that refuses it (a warning, once per model and process)."""
    if temperature is None or not _NO_TEMPERATURE.search(_claude_id(model)):
        return temperature
    if model not in _WARNED_TEMPERATURE:
        _WARNED_TEMPERATURE.add(model)
        logger.warning("%s does not take a temperature: sending the request without it", model)
    return None


def openai_effort(model: str) -> str | None:
    effort = current()
    if not effort or not _OPENAI_EFFORT.search(model.lower()):
        return None
    return "high" if effort in ("xhigh", "max") else effort
