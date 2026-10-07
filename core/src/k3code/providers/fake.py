# Vendored: none. k3code-original, first-party MIT.
"""Scripted fake provider for tests and offline smoke runs.

Set ``K3CODE_FAKE_PROVIDER`` to a JSON script file (absolute, or relative to
cwd) and every provider built from config becomes a FakeProvider that plays
the script back. The script is a list of steps applied in order to *every*
``stream()`` call:

    [
      {"type": "text", "text": "Hello"},
      {"type": "tool_call", "id": "call_1", "name": "bash",
       "arguments": {"command": "echo hi"}},
      {"type": "usage", "prompt_tokens": 12, "completion_tokens": 3},
      {"type": "error", "status_code": 500,
       "message": "provider on fire", "headers": {"retry-after": "1"}}
    ]

``text``/``tool_call``/``usage`` accumulate into one assistant turn (a final
done event); ``error`` aborts the stream with a ProviderError so the router
classifies and fails over. This makes full agent-loop runs testable with zero
network: the bash tool really executes, the loop really persists, and the
gateway really streams — only the model is canned.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from k3code.providers.base import Provider, ProviderError
from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec

logger = logging.getLogger(__name__)

FAKE_PROVIDER_ENV = "K3CODE_FAKE_PROVIDER"


class FakeProvider(Provider):
    """Replays a scripted step list instead of calling a network endpoint."""

    def __init__(self, *, name: str = "fake", steps: list[dict[str, Any]]) -> None:
        self.name = name
        self.base_url = "fake://script"
        self.api_key = ""
        self.steps = steps
        self.calls = 0

    def __repr__(self) -> str:
        return f"FakeProvider(name={self.name!r}, steps={len(self.steps)})"

    async def aclose(self) -> None:
        return None

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        *,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        logger.info("fake provider: replaying %d steps (call %d)", len(self.steps), self.calls)
        content: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: tuple[int, int] | None = None

        for step in self.steps:
            kind = step.get("type")
            if kind == "text":
                text = str(step.get("text", ""))
                content.append(text)
                yield StreamEvent(type="text_delta", text=text)
            elif kind == "tool_call":
                tool_calls.append(
                    ToolCall(
                        id=str(step.get("id") or f"call_{len(tool_calls) + 1}"),
                        name=str(step.get("name") or "bash"),
                        arguments=dict(step.get("arguments") or {}),
                        raw_arguments=step.get("raw_arguments"),
                    )
                )
                yield StreamEvent(type="tool_call", tool_call=tool_calls[-1])
            elif kind == "usage":
                usage = (int(step.get("prompt_tokens") or 0), int(step.get("completion_tokens") or 0))
            elif kind == "error":
                raise ProviderError(
                    message=str(step.get("message") or "fake provider error"),
                    status_code=step.get("status_code"),
                    headers={str(k): str(v) for k, v in (step.get("headers") or {}).items()},
                    body=dict(step.get("body") or {}),
                )
            else:
                raise ValueError(f"fake provider script: unknown step type {kind!r}")

        prompt, completion = usage or (0, 0)
        from k3code.providers.types import Usage

        final = Message(
            role="assistant",
            content="".join(content) or None,
            tool_calls=tool_calls,
            usage=Usage(prompt_tokens=prompt, completion_tokens=completion) if usage else None,
        )
        yield StreamEvent(type="done", message=final, usage=final.usage)


def load_fake_steps(path: str | Path) -> list[dict[str, Any]]:
    """Read a fake-provider script; raises FileNotFoundError/ValueError loudly."""
    script = Path(path).expanduser()
    if not script.is_file():
        raise FileNotFoundError(f"{FAKE_PROVIDER_ENV} points at a missing file: {script}")
    data = json.loads(script.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(step, dict) for step in data):
        raise ValueError(f"{FAKE_PROVIDER_ENV} script must be a JSON list of step objects: {script}")
    return data


def fake_provider_requested() -> str | None:
    """The K3CODE_FAKE_PROVIDER path, when set and non-empty."""
    return os.environ.get(FAKE_PROVIDER_ENV, "").strip() or None
