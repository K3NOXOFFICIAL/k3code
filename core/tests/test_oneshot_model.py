"""One-shot calls (/review, goal judge) must send the provider's mapped model, never the tier key itself.

Regression: ``oneshot`` passed ``model=<tier key>`` to the router as an explicit override, so an OpenAI-style
provider received the literal model name "default" (OmniRoute: "Unable to determine provider for model 'default'").
A claude-cli provider accepted "default" as an alias, which hid the bug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.providers.base import Provider
from k3code.providers.types import Message, StreamEvent, ToolSpec, Usage


class Recording(Provider):
    name = "rec"
    base_url = ""
    api_key = ""

    def __init__(self) -> None:
        self.models: list[str] = []

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
        self.models.append(model)
        final = Message(role="assistant", content="reviewed", usage=Usage())
        yield StreamEvent(type="text_delta", text="reviewed")
        yield StreamEvent(type="done", message=final, usage=Usage())


def _server(tmp_path: Path, provider: Recording) -> GatewayServer:
    entry = ProviderEntry(
        name="rec",
        kind="openai",
        base_url="http://127.0.0.1:9/v1",
        api_key_env="K",
        models={"default": "real-model-id", "cheap": "cheap-model-id"},
    )
    server = GatewayServer(config=Settings(providers=[entry]), store=SessionStore(tmp_path / "s.db"))
    server.providers = [provider]
    return server


async def test_oneshot_sends_the_mapped_model_not_the_key(tmp_path: Path) -> None:
    provider = Recording()
    server = _server(tmp_path, provider)
    assert await server.oneshot("sys", "user", model_key="default") == "reviewed"
    assert await server.oneshot("sys", "user", model_key="cheap") == "reviewed"
    assert await server.oneshot("sys", "user", model_key="not-a-key") == "reviewed"  # unknown key -> default model
    assert provider.models == ["real-model-id", "cheap-model-id", "real-model-id"]
