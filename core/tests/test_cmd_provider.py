"""/provider lists the provider blocks and switches the one a session's turns try first."""

from __future__ import annotations

import yaml

from k3code import chain_config
from k3code.config import ProviderEntry
from test_permissions_gateway import call, make_server, run_turn


def _two_blocks(server) -> None:
    server.config.providers = [
        ProviderEntry(name=n, kind="openai", base_url="http://127.0.0.1:9", api_key_env="NOPE", models={"default": m})
        for n, m in (("a", "m-a"), ("b", "m-b"))
    ]


async def _session(server, tmp_path) -> str:
    return (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]


async def _provider(server, sid: str | None, arg: str) -> str:
    return (await call(server, "command.dispatch", {"name": "provider", "arg": arg, "session_id": sid}))["message"]


async def test_bare_provider_lists_the_blocks_and_marks_the_active_one(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, [], monkeypatch)
    _two_blocks(server)
    sid = await _session(server, tmp_path)
    assert await _provider(server, sid, "") == "Providers (* = active):\n* a\n  b"
    await _provider(server, sid, "b")
    assert await _provider(server, sid, "") == "Providers (* = active):\n  a\n* b"
    # /help lists it with /model; command-name completion offers it
    cat = await call(server, "commands.catalog", {})
    group = next(c for c in cat["categories"] if c["name"] == "Model and settings")
    assert ["/provider", "List or switch the provider: /provider [<name> [--save]]"] in group["pairs"]
    done = await call(server, "complete.slash", {"text": "/prov"})
    assert [i["text"] for i in done["items"]] == ["/provider"]


async def test_switch_puts_the_block_first_from_the_next_turn_and_keeps_the_transcript(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    _two_blocks(server)
    sid = await _session(server, tmp_path)
    live = server._session_for(sid)
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    live.stored.messages = list(history)
    server.store.save(live.stored)

    assert await _provider(server, sid, "B") == "Provider set to: b (from the next turn)"  # names match in any case
    assert live.stored.provider == "b" and server.store.get(sid).provider == "b"
    assert server.store.get(sid).messages == history and live.stored.messages == history
    assert server.config.default_provider == ""  # a session switch is not the default

    # the next turn asks for the session's pick ...
    asked: list[str] = []
    real = server._ensure_router
    monkeypatch.setattr(server, "_ensure_router", lambda model=None, provider="": asked.append(provider) or real(model))
    await run_turn(server, "next", [])
    assert asked[0] == "b"
    monkeypatch.undo()
    # ... and gets a chain with b first, a still behind it as the fallback
    server._ensure_router(live.stored.model, live.stored.provider)
    assert [(e.provider_name, e.model) for e in server.router.chain] == [("b", "m-b"), ("a", "m-a")]
    assert server._active_model(live) == "m-b"
    server._ensure_router(live.stored.model, "")
    assert [e.provider_name for e in server.router.chain] == ["a", "b"]


async def test_unknown_provider_is_rejected_with_the_valid_names(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, [], monkeypatch)
    _two_blocks(server)
    sid = await _session(server, tmp_path)
    await _provider(server, sid, "a")
    assert await _provider(server, sid, "c") == "Unknown provider: c (valid: a, b)"
    assert await _provider(server, sid, "c --save") == "Unknown provider: c (valid: a, b)"
    assert server._session_for(sid).stored.provider == "a" and server.config.default_provider == ""
    assert not chain_config.config_path().exists()


async def test_save_writes_default_provider_to_the_user_config(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, [], monkeypatch)
    _two_blocks(server)
    path = chain_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("default_model: default\nmax_turns: 7\n")
    sid = await _session(server, tmp_path)
    out = await _provider(server, sid, "b --save")
    assert out.splitlines()[0] == "Provider set to: b (from the next turn)"
    assert out.splitlines()[1].startswith(f"Default provider: b (saved to {path}, backup: ")
    assert yaml.safe_load(path.read_text()) == {"default_model": "default", "max_turns": 7, "default_provider": "b"}
    assert server.config.default_provider == "b" and server._session_for(sid).stored.provider == "b"
    # a session without its own pick now starts on b too
    other = server._session_for(await _session(server, tmp_path))
    server._ensure_router(other.stored.model, other.stored.provider)
    assert server.router.chain[0].provider_name == "b"
