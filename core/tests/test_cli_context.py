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


def test_the_repl_keeps_the_conversation_across_prompts(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    seen: list[list[tuple[str, str]]] = []

    class Echo(Provider):
        def __init__(self) -> None:
            self.name, self.base_url, self.api_key = "echo", "fake://", ""

        async def aclose(self) -> None:
            return None

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            seen.append([(m.role, str(m.content or "")) for m in messages if m.role != "system"])
            reply = Message(role="assistant", content=f"answer {len(seen)}")
            yield StreamEvent(type="text_delta", text=reply.content)
            yield StreamEvent(type="done", message=reply, usage=Usage())

    import k3code.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [Echo()])
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    config = Settings(providers=[prov], default_model="default")
    inputs = iter(["my secret word is pineapple", "what was my secret word?", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    assert seen[0] == [("user", "my secret word is pineapple")]
    assert seen[1] == [
        ("user", "my secret word is pineapple"),
        ("assistant", "answer 1"),
        ("user", "what was my secret word?"),
    ]


def test_a_headless_run_clips_tool_results_to_context_tool_output_chars(tmp_path, monkeypatch):
    # the k3code home sits beside the project: a sandboxed bash refuses a working directory that contains it
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path.parent / f"{tmp_path.name}-k3home"))
    monkeypatch.chdir(tmp_path)
    sizes: list[int] = []

    class Printer(Provider):
        def __init__(self) -> None:
            self.name, self.base_url, self.api_key = "printer", "fake://", ""

        async def aclose(self) -> None:
            return None

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            results = [str(m.content or "") for m in messages if m.role == "tool"]
            if not results:
                call = ToolCall(id="b1", name="bash", arguments={"command": "yes xxxxxxxxxxxxxxxxxxxx | head -c 30000"})
                reply = Message(role="assistant", content=None, tool_calls=[call])
            else:
                sizes.append(len(results[-1]))
                reply = Message(role="assistant", content="done")
                yield StreamEvent(type="text_delta", text="done")
            yield StreamEvent(type="done", message=reply, usage=Usage())

    import k3code.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [Printer()])
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    config = Settings(providers=[prov], default_model="default", context={"tool_output_chars": 1_000})
    result = asyncio.run(
        cli_mod._run_headless("go", model=None, permission_mode=PermissionMode.YOLO, config=config, json_output=True)
    )
    assert result is not None and result.get("text") == "done", result
    assert sizes and sizes[0] < 1_400, f"a 30 000-character result clipped to the configured 1 000: {sizes}"


def test_the_repl_stops_on_repeated_tool_failures_like_the_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    requests: list[int] = []

    class Failing(Provider):
        def __init__(self) -> None:
            self.name, self.base_url, self.api_key = "failing", "fake://", ""

        async def aclose(self) -> None:
            return None

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            requests.append(len(messages))
            call = ToolCall(id=f"c{len(requests)}", name="read", arguments={"path": f"missing{len(requests)}.txt"})
            reply = Message(role="assistant", content=None, tool_calls=[call])
            yield StreamEvent(type="done", message=reply, usage=Usage())

    import k3code.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [Failing()])
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    config = Settings(providers=[prov], default_model="default", autonomy={"max_tool_errors": 3})
    inputs = iter(["go", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    assert len(requests) == 3, "three failed steps in a row end the turn, as they do in a gateway session"


def test_the_repl_takes_the_window_of_the_model_it_switches_to(tmp_path, monkeypatch):
    models = {"m": {"context_window": 2_000_000}, "m2": {"context_window": 40_000}}
    config, reader = _setup(monkeypatch, tmp_path, models=models)
    config.providers[0].models = {"default": "m", "small": "m2"}
    inputs = iter(["/model small", "go", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    assert max(reader.elided_markers) > 0, "the 40k window of the switched-to model is what the loop elides by"


def test_a_headless_run_that_repeats_one_call_ends_as_a_loop_guard_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path.parent / f"{tmp_path.name}-k3home"))
    monkeypatch.chdir(tmp_path)
    n = 0

    class Repeater(Provider):
        def __init__(self) -> None:
            self.name, self.base_url, self.api_key = "repeater", "fake://", ""

        async def aclose(self) -> None:
            return None

        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            nonlocal n
            n += 1
            call = ToolCall(id=f"c{n}", name="bash", arguments={"command": "echo same"})
            yield StreamEvent(
                type="done", message=Message(role="assistant", content=None, tool_calls=[call]), usage=Usage()
            )

    import k3code.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [Repeater()])
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    config = Settings(providers=[prov], default_model="default")
    result = asyncio.run(
        cli_mod._run_headless("go", model=None, permission_mode=PermissionMode.YOLO, config=config, json_output=True)
    )
    assert result is not None and result.get("error") == "loop_guard", result
    assert n < 10, "the guard ended it long before max_turns"
