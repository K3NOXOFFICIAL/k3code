"""`k3code -p` and the REPL give their loop the model's context window, so old tool results are elided from a long run
(they were resent whole on every request, and an overflow ended the run)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from k3code import cli as cli_mod
from k3code.config import ProviderEntry, Settings
from k3code.permissions import PermissionMode
from k3code.providers.base import Provider
from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec, Usage

READS = 14


class Reader(Provider):
    """Reads a different slice of one big file on each request, then answers; remembers what each request carried."""

    def __init__(self, path: str) -> None:
        self.name, self.base_url, self.api_key, self.path = "reader", "fake://", "", path
        self.tool_chars: list[int] = []
        self.elided_markers: list[int] = []

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
        results = [str(m.content or "") for m in messages if m.role == "tool"]
        self.tool_chars.append(sum(len(r) for r in results))
        self.elided_markers.append(sum(r.startswith("[earlier read result elided") for r in results))
        if len(results) < READS:
            call = ToolCall(
                id=f"c{len(results)}", name="read", arguments={"path": self.path, "offset": len(results) + 1}
            )
            reply = Message(role="assistant", content=None, tool_calls=[call])
        else:
            reply = Message(role="assistant", content="done reading")
            yield StreamEvent(type="text_delta", text="done reading")
        yield StreamEvent(type="done", message=reply, usage=Usage())


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra) -> tuple[Settings, Reader]:
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    big = tmp_path / "big.txt"
    big.write_text("a line of text that is long enough to count\n" * 700)  # ~30 kB, ~7.5k tokens per read
    reader = Reader(str(big))
    import k3code.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [reader])
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    return Settings(providers=[prov], default_model="default", **extra), reader


def test_a_headless_run_elides_old_tool_results_when_the_window_is_small(tmp_path, monkeypatch):
    config, reader = _setup(monkeypatch, tmp_path, models={"m": {"context_window": 40_000}})
    result = asyncio.run(
        cli_mod._run_headless("go", model=None, permission_mode=PermissionMode.YOLO, config=config, json_output=True)
    )
    assert result is not None and result.get("text") == "done reading", result
    assert max(reader.elided_markers) > 0
    # without elision the last request would carry all 14 results (~400 kB); with it, a few whole ones and markers
    assert reader.tool_chars[-1] < 4 * 30_000


def test_the_repl_loop_gets_the_window_too(tmp_path, monkeypatch):
    config, reader = _setup(monkeypatch, tmp_path, models={"m": {"context_window": 40_000}})
    inputs = iter(["go", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    assert max(reader.elided_markers) > 0
