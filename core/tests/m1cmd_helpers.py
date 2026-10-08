"""Shared helpers for the M1-commands tests."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.providers.types import Message, StreamEvent, Usage
from k3code.router import Router, build_chain


class TextProvider:
    """Each stream() plays the next scripted text reply (repeating the last one)."""

    name = "fake"
    base_url = "fake://"

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = replies or ["ok"]
        self.n = 0
        self.seen: list[list[Message]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.seen.append(list(messages))
        text = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        yield StreamEvent(type="text_delta", text=text)
        yield StreamEvent(type="done", message=Message(role="assistant", content=text, tool_calls=[]), usage=Usage())

    async def aclose(self) -> None:
        return None


def make_server(
    tmp: Path, monkeypatch, replies: list[str] | None = None, **settings
) -> tuple[GatewayServer, TextProvider]:
    # the k3code home sits beside the project dir: a sandboxed project may not contain it
    monkeypatch.setenv("K3CODE_HOME", str(tmp.parent / f"{tmp.name}-k3home"))
    store = SessionStore(tmp / "sessions.db")
    config = Settings(
        providers=[ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE")], **settings
    )
    server = GatewayServer(config=config, store=store)
    frames: list[str] = []
    server._write = frames.append  # type: ignore[method-assign]
    server._frames = frames  # type: ignore[attr-defined]
    provider = TextProvider(replies)
    server.providers = [provider]  # type: ignore[list-item]
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)  # type: ignore[list-item]
    server._chain_key = config.default_model
    server._oneshot_routers = {}  # type: ignore[attr-defined]
    server._oneshot_routers["default"] = server.router  # type: ignore[attr-defined]
    return server, provider


def frames_of(server: GatewayServer) -> list[dict]:
    return [json.loads(x) for x in server._frames]  # type: ignore[attr-defined]


async def rpc(server: GatewayServer, method: str, params: dict | None = None) -> dict:
    n = len(server._frames)  # type: ignore[attr-defined]
    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": 99, "method": method, "params": params or {}}))
    out = [f for f in frames_of(server)[n:] if f.get("id") == 99]
    assert out, "no response"
    return out[0]


async def new_session(server: GatewayServer, cwd: Path) -> str:
    res = (await rpc(server, "session.create", {"cwd": str(cwd)}))["result"]
    return res["session_id"]


async def cmd(server: GatewayServer, line: str, sid: str | None = None) -> dict:
    """Run a slash command through the gateway's ``slash.exec`` (same path the TUI uses)."""
    res = await rpc(server, "slash.exec", {"command": line.lstrip("/"), "session_id": sid})
    assert "result" in res, res
    return res["result"]


async def submit_and_wait(server: GatewayServer, text: str) -> None:
    await rpc(server, "prompt.submit", {"text": text})
    task = server.session.turn_task  # type: ignore[union-attr]
    await asyncio.wait_for(task, 20)


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*a: str) -> None:
        subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (path / "a.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    return path
