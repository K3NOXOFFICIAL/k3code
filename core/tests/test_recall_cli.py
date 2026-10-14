"""`k3code -p` and the REPL register the `recall` tools and say so in the prompt; a sub-agent has neither."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from k3code import cli as cli_mod
from k3code.config import ProviderEntry, Settings
from k3code.gateway.sessions import SessionStore
from k3code.permissions import PermissionMode
from k3code.prompting import RECALL_NOTE
from k3code.providers.base import Provider
from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec, Usage
from k3code.subagents.runner import Handle, child_reliability
from k3code.subagents.types import load_agent_types
from m1cmd_helpers import git_repo, make_server, new_session


class Recaller(Provider):
    """Calls `recall` once, then answers; remembers the tool names, the system prompt and the tool result."""

    def __init__(self) -> None:
        self.name, self.base_url, self.api_key = "recaller", "fake://", ""
        self.tools: list[str] = []
        self.system = ""
        self.result = ""

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
        if not results:
            self.tools = [t.name for t in tools]
            self.system = "\n".join(str(m.content or "") for m in messages if m.role == "system")
            call = ToolCall(id="r1", name="recall", arguments={"query": "vaultwarden rotation"})
            reply = Message(role="assistant", content=None, tool_calls=[call])
        else:
            self.result = results[0]
            reply = Message(role="assistant", content="recalled")
            yield StreamEvent(type="text_delta", text="recalled")
        yield StreamEvent(type="done", message=reply, usage=Usage())


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Settings, Recaller, Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    provider = Recaller()
    import k3code.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [provider])
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    return Settings(providers=[prov], default_model="default"), provider, home


def test_a_headless_run_can_recall_an_earlier_session(tmp_path, monkeypatch):
    config, provider, home = _setup(monkeypatch, tmp_path)
    home.mkdir()
    store = SessionStore(home / "sessions.db")
    s = store.create(cwd=str(tmp_path / "elsewhere"), title="ops")
    s.messages = [{"role": "user", "content": "the staging vaultwarden rotation runs nightly"}]
    store.save(s)
    store.close()
    result = asyncio.run(
        cli_mod._run_headless("go", model=None, permission_mode=PermissionMode.DEFAULT, config=config, json_output=True)
    )
    assert result is not None and result.get("text") == "recalled", result
    assert "recall" in provider.tools and "session_read" in provider.tools
    assert provider.system.count(RECALL_NOTE) == 1
    assert f"[{s.session_id}:0]" in provider.result and "nightly" in provider.result


def test_the_repl_has_recall_too_even_before_any_session_was_stored(tmp_path, monkeypatch):
    config, provider, home = _setup(monkeypatch, tmp_path)
    inputs = iter(["go", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.DEFAULT, config=config))
    assert "recall" in provider.tools and "session_read" in provider.tools
    assert provider.system.count(RECALL_NOTE) == 1
    # a fresh home: the tool answers with the sqlite error it caught (nothing else escapes it) and the run goes on
    assert provider.result == "Error: recall index unavailable: no such table: message_archive"


async def test_a_sub_agent_has_neither_the_tool_nor_the_sentence(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    sid = await new_session(server, repo)
    live = server.live_for(server.store.get(sid))
    assert RECALL_NOTE in server._system_prompt_for(live)  # the session's own loop has the tool, and is told
    for kind in ("worker", "explorer"):
        atype = load_agent_types(str(repo))[kind]
        h = Handle(id="sa-1", description="look", agent_type=kind, tier="main", depth=1, parent_sid=sid)
        reliability = child_reliability(server.config, "sa-1", server._home())
        loop = server.subagents.build_loop(live, h, atype, repo, reliability)
        assert "recall" not in loop.tools.names(), kind
        assert RECALL_NOTE not in f"{loop.system_prompt}\n\n{loop.system_suffix}", kind
    await server.close()
